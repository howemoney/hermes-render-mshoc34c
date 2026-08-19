#!/opt/hermes/.venv/bin/python
"""Idempotent patcher for Hermes config.yaml files on Render.

Runs on every boot from /etc/cont-init.d (016 for the root config, 017 for
the kanban worker/reviewer profile configs). It never has to be right about
the *whole* file — it only guarantees a short list of keys are present (or,
for a shorter list, exactly right) and leaves everything else alone.

Two modes
---------
  patch-config.py <path/to/config.yaml>
      ROOT mode (the gateway/dashboard config, normally /opt/data/config.yaml).
      Keeps the original two insertions and adds the kanban SDLC topology:
        1. mcp_servers.render -- HTTP MCP server pointed at mcp.render.com,
           authenticated via the RENDER_MCP_API_KEY env var. Hermes supports
           ${VAR} substitution in headers, so the key is resolved lazily at
           gateway startup. Users can rotate the key in Render's Environment
           tab without rebuilding the image. Registered without a
           `tools.include` filter, so Hermes can see every MCP tool the
           provided API key is allowed to use -- treat it as full Render
           account access and secure the dashboard/API key accordingly.
        2. skills.external_dirs -- exposes two pre-baked skill bundles to
           skills_list() and the / slash command surface, without colliding
           with the upstream skills_sync flow on /opt/data/skills:
             - /opt/render-tools/skills-local    (Hermes-on-Render overlay)
             - /opt/render-tools/skills-upstream (pinned render-oss/skills)
           The overlay does NOT shadow same-named upstream skills: Hermes
           refuses to guess between two skills of the same name (skill_view
           returns "Ambiguous skill name", tools/skills_tool.py:1355-1372 at
           v2026.8.18), so every overlay skill uses a distinct name.
        3. ROOT_ENFORCED / ROOT_INSERT_ONLY / ROOT_LIST_APPEND (below) --
           the kanban dispatcher topology, auxiliary models, delegation
           model and the render-kanban-guard plugin registration.

  patch-config.py --profile-config <path> [--profile coder|reviewer]
      PROFILE mode (/opt/data/profiles/<name>/config.yaml). Pins the
      profile's model/provider and registers the skills overlay + guard
      plugin for that profile. `--profile` may be omitted when the file
      lives in a directory named after the profile (the Hermes layout);
      an unknown profile name exits 2 with usage, so a typo in the boot
      hook fails loudly instead of silently pinning the wrong model.
      Profiles are declared in PROFILE_MODELS -- adding one is one line.

Tiers
-----
Every managed key belongs to exactly one tier:

  INSERT-ONLY  Written only when the key is absent (or explicitly null).
               An existing value -- even a different one, even an empty
               string -- wins. This is the default: users who edit
               config.yaml from the dashboard own those edits.
  ENFORCED     Overwritten on every boot when it differs, and the change
               is logged ("enforced <key> (<old> -> <new>)"). Reserved for
               topology/safety keys where a drift would silently break the
               SDLC contract (which profile dispatches, concurrency caps
               sized for the 4 GiB instance, which model a worker runs).
               Operators can still change them -- by editing this file and
               redeploying -- but not by hand-editing config.yaml.
  LIST-APPEND  For list-valued keys: the item is appended if missing and
               never removed or reordered (plugins.enabled,
               skills.external_dirs). Treated as "inserted" in the summary.

A non-mapping in the middle of a dotted path (e.g. `kanban: true`) is never
clobbered: the key is skipped with a warning on stderr, and the boot goes on.

Contract
--------
  - Idempotent: a second run against the same file changes nothing and
    writes nothing (the mtime stays put, so nothing downstream re-triggers).
  - Atomic: write to a sibling temp file, then rename over the original.
  - Never brick boot: a config that does not parse as YAML is left
    untouched and the patcher exits 0 (the wrapper cont-init hook also
    tolerates a non-zero exit). Unknown CLI usage exits 2 so a wiring bug
    in the Dockerfile/hook is visible in the deploy log.
  - Output: exactly one summary line on stdout listing the keys that were
    inserted and enforced, or a "nothing to do" line.
  - A config.yaml created from scratch here (fresh profile) carries no
    `_config_version`; Hermes treats that as a fresh minimal config and
    runs the normal migration ladder rather than the v12 support-floor
    refusal (hermes_cli/config.py:2296-2310 at v2026.8.18), so we do not
    stamp one.

Verified against upstream Hermes tag v2026.8.18 (hermes_cli/config_defaults.py
unless noted):

  key                                        line  tier      value here
  ------------------------------------------ ----- --------- -------------------
  kanban.dispatch_in_gateway                 2533  enforced  true
  kanban.orchestrator_profile                2555  enforced  "default"
  kanban.default_assignee                    2559  enforced  "coder"
  kanban.max_in_progress                     2572  enforced  2
  kanban.max_in_progress_per_profile         2581  enforced  1
  kanban.auto_decompose                      2586  enforced  false
  kanban.review_dispatch                     2537  insert    true
  kanban.dispatch_interval_seconds           2540  insert    60
  kanban.failure_limit                       2544  insert    2
  kanban.auto_decompose_per_tick             2590  insert    1
  kanban.dispatch_stale_timeout_seconds      2596  insert    14400
  kanban.reconcile_orphans                   2602  insert    true
  auxiliary.kanban_decomposer.{provider,model} 1087-1089 insert openrouter / deepseek-v4-flash-0731
  auxiliary.triage_specifier.{provider,model}  1073-1075 insert openrouter / deepseek-v4-flash-0731
  delegation.{model,provider}                1826-1827 insert deepseek-v4-pro-0813 / openrouter
  skills.external_dirs                       1966  append    (see RENDER_SKILL_DIRS)
  model.default / model.provider             (profile configs; canonical shape per
                                             hermes_cli/config.py:2860-2985 -- a bare
                                             `model: "<id>"` string is the documented
                                             shorthand for model.default, :5381)
  plugins.enabled                            not in DEFAULT_CONFIG; opt-in allow-list
                                             read from the raw config by
                                             hermes_cli/plugins.py:586-612
  plugins.entries.<id>.settings.*            hermes_cli/plugins.py:1423-1445
                                             (PluginContext.get_config)

Uses PyYAML, which ships with Hermes' .venv.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

# Skills overlay dirs. Both are listed for the root config; profile configs
# get only skills-local (the upstream render-oss bundle is for the dashboard
# operator, not for code workers). Names never collide between the two dirs
# -- see the module docstring for why order does not buy shadowing.
RENDER_SKILL_DIRS = (
    "/opt/render-tools/skills-local",
    "/opt/render-tools/skills-upstream",
)
RENDER_SKILLS_LOCAL = RENDER_SKILL_DIRS[0]
RENDER_MCP_URL = "https://mcp.render.com/mcp"
RENDER_MCP_AUTH = "Bearer ${RENDER_MCP_API_KEY}"

# The guard plugin (plugins/render-kanban-guard) is bundled into the image at
# /opt/hermes/plugins/render-kanban-guard and is opt-in via plugins.enabled
# in every HERMES_HOME that should load it: the root (dispatcher) and each
# worker/reviewer profile. Its settings are read per HERMES_HOME too, so the
# same settings block is written to every config this patcher touches.
GUARD_PLUGIN_ID = "render-kanban-guard"
ANCHOR_REPOS = ("/opt/data/work/stopsargassum",)
WORKER_PROFILES = ("coder",)
REVIEWER_PROFILES = ("reviewer",)
_GUARD_SETTINGS_PREFIX = f"plugins.entries.{GUARD_PLUGIN_ID}.settings"
GUARD_SETTINGS_INSERT_ONLY: dict[str, Any] = {
    f"{_GUARD_SETTINGS_PREFIX}.anchor_repos": list(ANCHOR_REPOS),
    f"{_GUARD_SETTINGS_PREFIX}.worker_profiles": list(WORKER_PROFILES),
    f"{_GUARD_SETTINGS_PREFIX}.reviewer_profiles": list(REVIEWER_PROFILES),
}

# Models. Workers are cheap-and-capable; the reviewer/orchestrator is the
# strong model because it owns merges. The aux decomposer/specifier are the
# flash tier: short JSON-shaped calls where the main model is overkill.
WORKER_MODEL = "deepseek/deepseek-v4-pro-0813"
REVIEWER_MODEL = "openai/gpt-5.6-sol"
AUX_MODEL = "deepseek/deepseek-v4-flash-0731"
MODEL_PROVIDER = "openrouter"

# profile name -> (model.default, model.provider). Adding a profile is one
# line here plus its `hermes profile create` in the 017 boot hook.
PROFILE_MODELS: dict[str, tuple[str, str]] = {
    "coder": (WORKER_MODEL, MODEL_PROVIDER),
    "reviewer": (REVIEWER_MODEL, MODEL_PROVIDER),
}

# ---------------------------------------------------------------- ROOT tiers
ROOT_ENFORCED: dict[str, Any] = {
    # In-gateway dispatcher is the only supported one; the standalone
    # `hermes kanban daemon` is deprecated and contends for the lock.
    "kanban.dispatch_in_gateway": True,
    "kanban.orchestrator_profile": "default",
    "kanban.default_assignee": "coder",
    # 4 GiB instance: one coder + one reviewer at a time. Raise after metrics.
    "kanban.max_in_progress": 2,
    "kanban.max_in_progress_per_profile": 1,
    # Stops the triage -> decompose fan-out churn; decomposition is explicit.
    "kanban.auto_decompose": False,
}
ROOT_INSERT_ONLY: dict[str, Any] = {
    "kanban.review_dispatch": True,
    "kanban.dispatch_interval_seconds": 60,
    "kanban.failure_limit": 2,
    "kanban.auto_decompose_per_tick": 1,
    "kanban.dispatch_stale_timeout_seconds": 14400,
    "kanban.reconcile_orphans": True,
    "auxiliary.kanban_decomposer.provider": MODEL_PROVIDER,
    "auxiliary.kanban_decomposer.model": AUX_MODEL,
    "auxiliary.triage_specifier.provider": MODEL_PROVIDER,
    "auxiliary.triage_specifier.model": AUX_MODEL,
    "delegation.model": WORKER_MODEL,
    "delegation.provider": MODEL_PROVIDER,
    **GUARD_SETTINGS_INSERT_ONLY,
}
ROOT_LIST_APPEND: dict[str, list[Any]] = {
    "plugins.enabled": [GUARD_PLUGIN_ID],
}

# ------------------------------------------------------------- PROFILE tiers
# model.* is filled in per profile from PROFILE_MODELS at run time.
PROFILE_ENFORCED_TEMPLATE = ("model.default", "model.provider")
PROFILE_INSERT_ONLY: dict[str, Any] = {
    **GUARD_SETTINGS_INSERT_ONLY,
}
PROFILE_LIST_APPEND: dict[str, list[Any]] = {
    "skills.external_dirs": [RENDER_SKILLS_LOCAL],
    "plugins.enabled": [GUARD_PLUGIN_ID],
}


def _warn(msg: str) -> None:
    print(f"[render-tools] {msg}", file=sys.stderr)


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"cannot read {path}: {exc}")
        return {}
    if not raw.strip():
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        _warn(f"{path} is not valid YAML ({exc}); refusing to patch")
        sys.exit(0)
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ helpers

def _parent_mapping(config: dict, parts: list[str], dotted_path: str) -> dict | None:
    """Walk/create the mappings down to the parent of the leaf.

    Returns None (after a stderr warning) when an intermediate key exists but
    is not a mapping -- we never replace a user's scalar/list with a dict.
    An intermediate that is explicitly null (`kanban:` with nothing after it)
    is treated as absent and becomes a mapping.
    """
    node = config
    for depth, part in enumerate(parts[:-1]):
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            _warn(
                f"{'.'.join(parts[: depth + 1])} is not a mapping; "
                f"skipping {dotted_path}"
            )
            return None
        node = child
    return node


def _same_value(a: Any, b: Any) -> bool:
    # `True == 1` and `1.0 == 1` in Python; an enforced int must not be
    # satisfied by a bool (and vice versa), so compare types too.
    return type(a) is type(b) and a == b


def lookup(config: dict, dotted_path: str, default: Any = None) -> Any:
    """Read a dotted path without creating anything."""
    node: Any = config
    for part in dotted_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def ensure_nested(config: dict, dotted_path: str, value: Any, enforce: bool = False) -> bool:
    """Make sure `dotted_path` holds `value`.

    enforce=False (INSERT-ONLY): write only if the leaf is absent or null.
    enforce=True  (ENFORCED):    write whenever the leaf differs.
    Returns True iff the config was modified.
    """
    parts = dotted_path.split(".")
    parent = _parent_mapping(config, parts, dotted_path)
    if parent is None:
        return False
    leaf = parts[-1]
    current = parent.get(leaf)
    if current is not None:
        if not enforce or _same_value(current, value):
            return False
    parent[leaf] = copy.deepcopy(value)
    return True


def ensure_list_contains(config: dict, dotted_path: str, item: Any) -> bool:
    """Append `item` to the list at `dotted_path` if missing (LIST-APPEND).

    Creates the list when the leaf is absent/null. A non-list leaf is left
    alone with a warning. Returns True iff the config was modified.
    """
    parts = dotted_path.split(".")
    parent = _parent_mapping(config, parts, dotted_path)
    if parent is None:
        return False
    leaf = parts[-1]
    current = parent.get(leaf)
    if current is None:
        parent[leaf] = [copy.deepcopy(item)]
        return True
    if not isinstance(current, list):
        _warn(f"{dotted_path} is not a list; skipping append of {item!r}")
        return False
    if item in current:
        return False
    current.append(copy.deepcopy(item))
    return True


# --------------------------------------------------- original root insertions

def _render_entry() -> dict:
    return {
        "url": RENDER_MCP_URL,
        "headers": {"Authorization": RENDER_MCP_AUTH},
    }


def ensure_render_mcp(config: dict) -> bool:
    """Insert mcp_servers.render if missing. Returns True if changed."""
    mcp_servers = config.get("mcp_servers")
    if mcp_servers is None:
        config["mcp_servers"] = {"render": _render_entry()}
        return True
    if not isinstance(mcp_servers, dict):
        _warn("mcp_servers is not a mapping; skipping render entry")
        return False
    if "render" in mcp_servers:
        return False
    mcp_servers["render"] = _render_entry()
    return True


def ensure_external_skill_dirs(config: dict, dirs: tuple[str, ...] = RENDER_SKILL_DIRS) -> list[str]:
    """Append the render-tools skill dirs to skills.external_dirs if missing.

    Returns the list of paths that were actually added.
    """
    return [d for d in dirs if ensure_list_contains(config, "skills.external_dirs", d)]


# ---------------------------------------------------------------- tier driver

class PatchReport:
    """Collects what changed so main() can print one summary line."""

    def __init__(self) -> None:
        self.inserted: list[str] = []
        self.enforced: list[str] = []

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.enforced)

    def summary(self) -> str:
        parts = []
        if self.inserted:
            parts.append("inserted " + ", ".join(self.inserted))
        if self.enforced:
            parts.append("enforced " + ", ".join(self.enforced))
        return "; ".join(parts)


def _fmt(value: Any) -> str:
    # Values under the managed keys are model ids / bools / ints / short
    # lists -- never secrets -- so echoing them in the boot log is fine.
    # YAML-ish scalars (true/false/null, bare strings) so the log reads like
    # the file; json for anything structured.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def apply_tiers(
    config: dict,
    report: PatchReport,
    *,
    enforced: dict[str, Any],
    insert_only: dict[str, Any],
    list_append: dict[str, list[Any]],
) -> None:
    for key, value in enforced.items():
        before = lookup(config, key)
        if ensure_nested(config, key, value, enforce=True):
            if before is None:
                report.inserted.append(key)
            else:
                report.enforced.append(f"{key} ({_fmt(before)} -> {_fmt(value)})")
    for key, value in insert_only.items():
        if ensure_nested(config, key, value, enforce=False):
            report.inserted.append(key)
    for key, items in list_append.items():
        for item in items:
            if ensure_list_contains(config, key, item):
                report.inserted.append(f"{key} += {item}")


def patch_root(config: dict) -> PatchReport:
    report = PatchReport()
    if ensure_render_mcp(config):
        report.inserted.append("mcp_servers.render")
    for dir_path in ensure_external_skill_dirs(config):
        report.inserted.append(f"skills.external_dirs += {dir_path}")
    apply_tiers(
        config,
        report,
        enforced=ROOT_ENFORCED,
        insert_only=ROOT_INSERT_ONLY,
        list_append=ROOT_LIST_APPEND,
    )
    return report


def _normalise_bare_model(config: dict) -> None:
    """`model: "<id>"` is Hermes' documented shorthand for model.default
    (hermes_cli/config.py:5381 redirects it on `config set`). Promote it to
    the mapping form so the enforced model.* keys have somewhere to land
    instead of being skipped as "model is not a mapping"."""
    model = config.get("model")
    if isinstance(model, str):
        config["model"] = {"default": model} if model.strip() else {}


def profile_enforced(profile: str) -> dict[str, Any]:
    model, provider = PROFILE_MODELS[profile]
    return dict(zip(PROFILE_ENFORCED_TEMPLATE, (model, provider)))


def patch_profile(config: dict, profile: str) -> PatchReport:
    report = PatchReport()
    _normalise_bare_model(config)
    apply_tiers(
        config,
        report,
        enforced=profile_enforced(profile),
        insert_only=PROFILE_INSERT_ONLY,
        list_append=PROFILE_LIST_APPEND,
    )
    return report


# -------------------------------------------------------------------- I/O

def save_config(path: Path, config: dict) -> None:
    text = yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    tmp = path.with_suffix(path.suffix + ".render-tools.tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        # Upstream chmods config.yaml to 640 every boot; keep whatever mode
        # the file had rather than widening it to the umask default.
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
    tmp.replace(path)


def _infer_profile(path: Path) -> str | None:
    """/opt/data/profiles/<name>/config.yaml -> <name> (Hermes' layout)."""
    name = path.resolve().parent.name
    return name if name in PROFILE_MODELS else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch-config.py",
        description="Idempotent Hermes config.yaml patcher for Render (see module docstring).",
    )
    parser.add_argument(
        "root_config",
        nargs="?",
        metavar="<path/to/config.yaml>",
        help="ROOT mode: the gateway config to patch",
    )
    parser.add_argument(
        "--profile-config",
        metavar="<path>",
        help="PROFILE mode: a profile's config.yaml to patch",
    )
    parser.add_argument(
        "--profile",
        metavar="|".join(PROFILE_MODELS),
        help="profile name for --profile-config (default: inferred from the parent directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if bool(args.root_config) == bool(args.profile_config):
        parser.print_usage(sys.stderr)
        _warn("pass exactly one of <path/to/config.yaml> or --profile-config <path>")
        return 2
    if args.profile and not args.profile_config:
        parser.print_usage(sys.stderr)
        _warn("--profile only makes sense with --profile-config")
        return 2

    if args.profile_config:
        path = Path(args.profile_config)
        profile = args.profile or _infer_profile(path)
        if profile not in PROFILE_MODELS:
            parser.print_usage(sys.stderr)
            _warn(
                f"unknown profile {profile!r} (known: {', '.join(PROFILE_MODELS)}); "
                "add it to PROFILE_MODELS in patch-config.py"
            )
            return 2
        label = f"profile {profile}"
    else:
        path = Path(args.root_config)
        profile = None
        label = "root"

    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_config(path)
    report = patch_profile(config, profile) if profile else patch_root(config)

    if not report.changed:
        print(f"[render-tools] {path} already in shape ({label}); nothing to do")
        return 0
    try:
        save_config(path, config)
    except OSError as exc:
        _warn(f"cannot write {path}: {exc}; leaving it unmodified")
        return 1
    print(f"[render-tools] patched {path} ({label}): {report.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
