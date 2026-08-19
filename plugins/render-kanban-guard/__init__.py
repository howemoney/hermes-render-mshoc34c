"""render-kanban-guard — Howe Agency SDLC guard rails for Hermes Kanban on Render.

Why this plugin exists
----------------------
Observed on the live board (2026-08-19): the bot was committing straight to
``origin/main``, worktrees were being cut from a STALE anchor ``main`` (upstream
``_ensure_git_worktree`` runs ``git worktree add -b <branch> <target> HEAD`` on
the anchor, with no fetch — hermes_cli/kanban_db.py:7724-7728), and workers
were exiting "cleanly" without calling any ``kanban_*`` terminal tool
(protocol violations). Three small, process-local behaviours fix the
mechanical half of that; the prose half lives in the house skills
``kanban-sdlc-worker`` / ``kanban-sdlc-reviewer``.

What it wires (all verified against the deployed tag v2026.8.18)
----------------------------------------------------------------
1. ``kanban_task_claimed`` hook -> :func:`on_kanban_task_claimed`
   Fires in the DISPATCHER process (plugins.py:261-265) from
   ``claim_task`` AFTER the claim txn commits (kanban_db.py:4729-4735) and
   BEFORE the dispatch loop resolves the worktree:
   ``claim_task`` kanban_db.py:10231 -> ``_resolve_worktree_workspace``
   :10236 -> ``_ensure_git_worktree(... HEAD)`` :7713-7742. So if we
   fast-forward the anchor's local ``main`` to ``origin/main`` inside this
   hook, the new ``wt/<id>`` worktree is cut from FRESH main. Kwargs:
   ``task_id, profile_name, board, assignee, run_id`` (+ the additive
   ``telemetry_schema_version``; plugins.py:5102). Note ``claim_review_task``
   (kanban_db.py:4746+) does NOT fire this hook — fine, the reviewer reuses
   the implementer's existing worktree/branch. The hook runs inside the
   board's in-process dispatch lock (plugins.py:303-304: "callbacks must stay
   fast"), NOT the SQLite write lock; we accept holding it for one bounded
   ``git fetch`` (45 s cap, once per 30 s per repo) because a stale anchor
   costs far more (every worktree starts behind main -> merge round-trips).

2. ``pre_tool_call`` hook -> :func:`on_pre_tool_call`
   Invoked for EVERY tool (agent/tool_executor.py:605-618 ->
   ``_dispatch_pre_tool_call_hooks`` plugins.py:6210 ->
   ``_get_pre_tool_call_directive_details`` :5968) with kwargs
   ``tool_name, args, task_id, session_id, tool_call_id, turn_id,
   api_request_id, middleware_trace`` (plugins.py:6012-6022). Return
   ``{"action": "block", "message": "..."}`` to veto (the message becomes
   the tool result the model sees; a block without a message is ignored,
   :6063-6066) or ``None`` to allow. The command text lives in
   ``args["command"]`` for ``terminal`` (tools/terminal_tool.py:3863-3904,
   required key ``command``) and ``args["code"]`` for ``execute_code``
   (tools/code_execution_tool.py:2143-2158, required key ``code``).

3. ``register_system_prompt_section("render-kanban-sdlc-role", provider,
   max_chars=1500)`` (plugins.py:3134-3141). The provider receives a
   read-only mapping with keys ``session_id, model, provider, platform,
   profile_name, cwd`` (agent/system_prompt.py:159-186). Empty/whitespace
   output is silently dropped (plugins.py:5360-5362) and anything over
   ``max_chars`` (cap 4000, :496-497) is skipped with a warning, so the
   texts below are kept well under budget. Rendered once per NEW session and
   frozen into the persisted prompt (:189-200) — exactly what a one-shot
   kanban worker run needs.

Process model (why every handler checks ``HERMES_KANBAN_TASK``)
-------------------------------------------------------------
The dispatcher spawns each worker as ``hermes -p <assignee> chat -q`` with
``HERMES_KANBAN_TASK=<id>``, ``HERMES_KANBAN_WORKSPACE``, ``HERMES_PROFILE=
<assignee>`` etc. (kanban_db.py:10764-10831). The gateway/dispatcher itself
never has ``HERMES_KANBAN_TASK`` set. So:
  - anchor sync runs only when the var is ABSENT (dispatcher),
  - command blocking + the prompt section run only when it is PRESENT.
Both the root config and each kanban profile config list this plugin in
``plugins.enabled`` (bundled standalone plugins are opt-in, plugins.py:
3967-3984) so both processes load it.

Settings (``plugins.entries.render-kanban-guard.settings.*``, read once at
``register()`` via ``ctx.get_config`` — plugins.py:1422-1448):
  anchor_repos        [/opt/data/work/stopsargassum]
  worker_profiles     [coder]
  reviewer_profiles   [reviewer]
  protected_branches  [main, master]

Design rules: never raise out of a hook (a raising hook is only logged by
the manager, plugins.py:5105-5112, but we still want our own WARNING with
context), never block boot, never print secrets, keep the pure functions
(:func:`classify_command`, :func:`sync_anchor`, :func:`role_prompt_section`)
importable WITHOUT the Hermes runtime so ``tests/test_kanban_guard_plugin.py``
can drive them with plain ``unittest``.

This is an ACCIDENT guard for LLM workers, not a security boundary: a worker
can still write a shell script and run it. The git ``pre-push`` hook on the
anchor (scripts/git-hooks/pre-push) is the second layer; the reviewer's
``gh pr merge`` is the only sanctioned path to ``main``.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

LOG_PREFIX = "[render-kanban-guard]"

# ---------------------------------------------------------------------------
# Defaults (mirrored in plugin.yaml config_schema and in patch-config.py)
# ---------------------------------------------------------------------------

DEFAULT_ANCHOR_REPOS: tuple[str, ...] = ("/opt/data/work/stopsargassum",)
DEFAULT_WORKER_PROFILES: tuple[str, ...] = ("coder",)
DEFAULT_REVIEWER_PROFILES: tuple[str, ...] = ("reviewer",)
DEFAULT_PROTECTED_BRANCHES: tuple[str, ...] = ("main", "master")

# Tools whose string payload is a shell command / Python source we inspect.
# tool name -> arg key (verified: terminal_tool.py:3870 / code_execution_tool.py:2148)
COMMAND_TOOLS: Dict[str, str] = {
    "terminal": "command",
    "execute_code": "code",
}

# Anchor sync knobs (plan B3).
ANCHOR_FETCH_TIMEOUT_S = 45
ANCHOR_GIT_TIMEOUT_S = 30          # for the cheap local git calls
ANCHOR_SYNC_MIN_INTERVAL_S = 30.0  # per-repo rate limit

SYSTEM_PROMPT_SECTION_ID = "render-kanban-sdlc-role"
SYSTEM_PROMPT_SECTION_MAX_CHARS = 1500

# ---------------------------------------------------------------------------
# Settings (resolved once at register(); pure functions take them as args)
# ---------------------------------------------------------------------------


class Settings:
    """Plain holder so hooks don't re-read config.yaml on every tool call."""

    __slots__ = ("anchor_repos", "worker_profiles", "reviewer_profiles", "protected_branches")

    def __init__(
        self,
        anchor_repos: Iterable[str] = DEFAULT_ANCHOR_REPOS,
        worker_profiles: Iterable[str] = DEFAULT_WORKER_PROFILES,
        reviewer_profiles: Iterable[str] = DEFAULT_REVIEWER_PROFILES,
        protected_branches: Iterable[str] = DEFAULT_PROTECTED_BRANCHES,
    ) -> None:
        self.anchor_repos = _str_list(anchor_repos, DEFAULT_ANCHOR_REPOS)
        self.worker_profiles = _str_list(worker_profiles, DEFAULT_WORKER_PROFILES)
        self.reviewer_profiles = _str_list(reviewer_profiles, DEFAULT_REVIEWER_PROFILES)
        self.protected_branches = [
            b.lower() for b in _str_list(protected_branches, DEFAULT_PROTECTED_BRANCHES)
        ]


def _str_list(value: Any, default: Sequence[str]) -> List[str]:
    """Coerce a config value to a non-empty list of stripped strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return list(default)
    out = [str(v).strip() for v in value if str(v).strip()]
    return out or list(default)


def settings_from_ctx(ctx: Any) -> Settings:
    """Build :class:`Settings` from ``ctx.get_config``; any failure -> defaults.

    ``ctx.get_config(key)`` reads ``plugins.entries.<id>.settings.<key>``
    (plugins.py:1422-1448) and raises ValueError only for malformed keys —
    ours are plain identifiers. Wrapped anyway: a config read error must not
    stop the plugin from registering its guards with safe defaults.
    """
    def _get(key: str, default: Sequence[str]) -> Any:
        try:
            value = ctx.get_config(key, None)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("%s get_config(%s) failed: %s; using default", LOG_PREFIX, key, exc)
            return list(default)
        return default if value is None else value

    return Settings(
        anchor_repos=_get("anchor_repos", DEFAULT_ANCHOR_REPOS),
        worker_profiles=_get("worker_profiles", DEFAULT_WORKER_PROFILES),
        reviewer_profiles=_get("reviewer_profiles", DEFAULT_REVIEWER_PROFILES),
        protected_branches=_get("protected_branches", DEFAULT_PROTECTED_BRANCHES),
    )


_SETTINGS: Settings = Settings()


def _in_kanban_worker(environ: Mapping[str, str] = os.environ) -> bool:
    """True inside a dispatcher-spawned worker (kanban_db.py:10764)."""
    return bool(environ.get("HERMES_KANBAN_TASK", "").strip())


def _current_profile(
    environ: Mapping[str, str] = os.environ,
    session_info: Optional[Mapping[str, Any]] = None,
) -> str:
    """Resolve the acting profile name.

    ``HERMES_PROFILE`` is set explicitly by the dispatcher to the card's
    assignee (kanban_db.py:10827-10831) and is the same value the kanban
    tools use for attribution, so it wins. ``session_info["profile_name"]``
    (agent/system_prompt.py:168-178, derived from HERMES_HOME) is the
    fallback for the prompt section; "custom" is what it returns when
    HERMES_HOME is an unrecognised path (profiles.py:1918-1942) — treat
    that as unknown.
    """
    env_profile = (environ.get("HERMES_PROFILE") or "").strip()
    if env_profile:
        return env_profile
    if session_info:
        name = str(session_info.get("profile_name") or "").strip()
        if name and name != "custom":
            return name
    return ""


# ---------------------------------------------------------------------------
# 1. Command classifier (pure)
# ---------------------------------------------------------------------------

# Segment separators: we classify each simple command on its own so that
# `git fetch origin && git push origin main` still trips on the second half.
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\||\n)\s*")
_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n")
_QUOTE_CHARS_RE = re.compile(r"""["'`]""")
_PY_PUNCT_RE = re.compile(r"[\[\](){},]")
_MERGE_API_RE = re.compile(r"/?pulls/[^/\s]+/merge\b", re.IGNORECASE)
_GH_PR_MERGE_RE = re.compile(r"(?:^|\s)gh\s+pr\s+merge(?:\s|$)", re.IGNORECASE)

# `git <global opts> push`: global options that take a separate value
# (git(1): -C <path>, -c <name=value>, --git-dir <path>, --work-tree <path>,
# --namespace <name>, --exec-path <path> when not given as --x=y).
_GIT_GLOBAL_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# `git push` options that may take a SEPARATE value token (git-push(1):
# OPT_STRING / OPT_STRING_LIST). `--recurse-submodules` and `--signed` take
# an optional `=value` only, so they are deliberately absent — treating them
# as value-taking would swallow the remote/refspec that follows.
_PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
# Short-flag cluster containing `f` (e.g. -f, -fu, -uf). `--force` is matched
# by exact token; `--force-with-lease[=...]` / `--force-if-includes` are the
# sanctioned safe forms and are NOT blocked.
_SHORT_FORCE_RE = re.compile(r"^-[a-zA-Z]*f[a-zA-Z]*$")


def _normalize(text: str, *, python: bool) -> str:
    text = _LINE_CONTINUATION_RE.sub(" ", text)
    text = _QUOTE_CHARS_RE.sub("", text)
    if python:
        # subprocess.run(["git", "push", "origin", "main"]) ->
        #  subprocess.run  git  push  origin  main
        text = _PY_PUNCT_RE.sub(" ", text)
    return text


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _is_git_token(tok: str) -> bool:
    return tok == "git" or tok.endswith("/git")


def _norm_path(p: str) -> str:
    p = os.path.expanduser(p)
    return os.path.normpath(p)


def _refspec_dst(spec: str) -> str:
    """`src:dst` -> dst; `dst` -> dst; strip refs/heads/ and a leading +."""
    spec = spec.lstrip("+")
    if ":" in spec:
        spec = spec.split(":", 1)[1]
    if spec.startswith("refs/heads/"):
        spec = spec[len("refs/heads/"):]
    return spec


def _classify_git_push(
    tokens: List[str],
    git_idx: int,
    *,
    cwd_hint: Optional[str],
    anchor_repos: Sequence[str],
    protected: Sequence[str],
) -> Optional[str]:
    """Return a block reason for the `git ... push ...` starting at git_idx, else None."""
    i = git_idx + 1
    c_path: Optional[str] = None
    # --- global git options ------------------------------------------------
    while i < len(tokens) and tokens[i].startswith("-"):
        tok = tokens[i]
        if tok in _GIT_GLOBAL_VALUE_OPTS:
            if tok == "-C" and i + 1 < len(tokens):
                c_path = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2 and not tok.startswith("--"):
            c_path = tok[2:]  # -C/path form
        i += 1
    if i >= len(tokens) or tokens[i] != "push":
        return None
    # --- where does this push run from? -----------------------------------
    effective_cwd = c_path if c_path is not None else cwd_hint
    if effective_cwd:
        normalized = _norm_path(effective_cwd)
        for anchor in anchor_repos:
            if normalized == _norm_path(anchor):
                return (
                    f"push from the anchor checkout {anchor} is not allowed "
                    "(its local main is dispatcher-managed). Work and push from your "
                    "task worktree ($HERMES_KANBAN_WORKSPACE) on your wt/* branch."
                )
    # --- push arguments ----------------------------------------------------
    positional: List[str] = []
    j = i + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok == "--":
            positional.extend(tokens[j + 1:])
            break
        if tok.startswith("-"):
            if tok == "--no-verify":
                return (
                    "`git push --no-verify` is not allowed: it bypasses the anchor's "
                    "pre-push hook. Push without --no-verify; if the hook rejects "
                    "you, you are pushing to a protected branch."
                )
            if tok == "--force" or tok == "--force=true" or _SHORT_FORCE_RE.match(tok):
                return (
                    "`git push --force` / `-f` is not allowed: rewriting a pushed "
                    "branch loses history the reviewer relies on. If you truly need "
                    "to replace your own wt/* branch tip use "
                    "`git push --force-with-lease origin <branch>`, otherwise "
                    "`git fetch origin && git merge --no-edit origin/<branch>` and "
                    "push normally."
                )
            if tok in ("--all", "--mirror", "--branches"):
                return (
                    f"`git push {tok}` is not allowed: it pushes every local branch "
                    "(including main). Push only your task branch: "
                    "`git push -u origin <wt-branch>`."
                )
            if tok in _PUSH_VALUE_OPTS and "=" not in tok:
                j += 2
                continue
            j += 1
            continue
        positional.append(tok)
        j += 1
    # positional[0] is the remote; the rest are refspecs. `--delete main` and
    # `-d main` also land main in positional and are caught the same way.
    for spec in positional[1:]:
        if spec.startswith("+"):
            return (
                "`git push` with a `+refspec` is a force push and is not allowed. "
                "Push your wt/* branch normally or use --force-with-lease."
            )
        dst = _refspec_dst(spec).lower()
        if dst in protected:
            return (
                f"pushing to '{_refspec_dst(spec)}' is not allowed for kanban workers. "
                "Push your task branch (`git push -u origin $HERMES_KANBAN_BRANCH` / "
                "wt/<task>) and open or refresh the PR with `gh pr create`; the "
                "reviewer profile lands it on main via a squash-merge."
            )
    return None


def classify_command(
    text: str,
    *,
    profile: str = "",
    tool_name: str = "terminal",
    anchor_repos: Sequence[str] = DEFAULT_ANCHOR_REPOS,
    worker_profiles: Sequence[str] = DEFAULT_WORKER_PROFILES,
    reviewer_profiles: Sequence[str] = DEFAULT_REVIEWER_PROFILES,
    protected_branches: Sequence[str] = DEFAULT_PROTECTED_BRANCHES,
) -> Optional[str]:
    """Return a human-readable block reason, or ``None`` when the command is fine.

    Pure: no env, no subprocess. ``profile`` decides whether the merge rules
    apply (only for ``worker_profiles``). ``tool_name='execute_code'`` turns
    on the looser Python normalisation (brackets/commas -> spaces) so
    ``subprocess.run(["git","push","origin","main"])`` is still caught.
    Blocking is deliberately regex/token based and case-insensitive for
    branch names; it tolerates quoting, extra whitespace, line continuations,
    `VAR=x git push`, `/usr/bin/git push`, `git -C <path> push` and the
    `cd <anchor> && git push` shape.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    protected = [b.lower() for b in protected_branches]
    is_worker = profile in set(worker_profiles)
    normalized = _normalize(text, python=(tool_name == "execute_code"))

    # Merge rules (workers only). Reviewers are the sanctioned mergers.
    if is_worker:
        if _GH_PR_MERGE_RE.search(normalized):
            return (
                "`gh pr merge` is not allowed for the worker profile: merging is the "
                "reviewer's job. Finish with kanban_request_review(reviewer=\"reviewer\", "
                "summary=..., metadata={pr_url, head_sha, ...}) and stop."
            )
        if _MERGE_API_RE.search(normalized):
            return (
                "calling the GitHub pull-request merge API is not allowed for the worker "
                "profile: merging is the reviewer's job. Finish with "
                "kanban_request_review(reviewer=\"reviewer\", ...) and stop."
            )

    cwd_hint: Optional[str] = None
    for segment in _SEGMENT_SPLIT_RE.split(normalized):
        if not segment.strip():
            continue
        tokens = _tokens(segment)
        if not tokens:
            continue
        # Track `cd <path>` so a later bare `git push` in the same command
        # string is attributed to that directory.
        if tokens[0] in ("cd", "pushd") and len(tokens) >= 2:
            cwd_hint = tokens[1]
            continue
        for idx, tok in enumerate(tokens):
            if not _is_git_token(tok):
                continue
            reason = _classify_git_push(
                tokens, idx,
                cwd_hint=cwd_hint,
                anchor_repos=anchor_repos,
                protected=protected,
            )
            if reason:
                return reason
    return None


# ---------------------------------------------------------------------------
# 2. Anchor sync (pure-ish: injectable runner + clock)
# ---------------------------------------------------------------------------

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]

# Per-repo last-attempt stamps (monotonic seconds). Module-level on purpose:
# the dispatcher is one long-lived process and claims can burst (several
# cards dispatched in one tick), so one fetch per 30 s per repo is plenty.
_LAST_SYNC_ATTEMPT: Dict[str, float] = {}


def _default_run(
    argv: Sequence[str], *, cwd: str, timeout: int, env: Optional[Mapping[str, str]] = None
) -> "subprocess.CompletedProcess[str]":
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=merged,
    )


def _git(run: RunFn, repo: str, *args: str, timeout: int = ANCHOR_GIT_TIMEOUT_S,
         env: Optional[Mapping[str, str]] = None) -> "subprocess.CompletedProcess[str]":
    return run(["git", *args], cwd=repo, timeout=timeout, env=env)


def sync_anchor(
    repo: str,
    *,
    run: Optional[RunFn] = None,
    now: Optional[Callable[[], float]] = None,
    min_interval: float = ANCHOR_SYNC_MIN_INTERVAL_S,
    fetch_timeout: int = ANCHOR_FETCH_TIMEOUT_S,
    log: logging.Logger = logger,
) -> str:
    """Fast-forward ``<repo>``'s local ``main`` to ``origin/main``.

    Returns a short status token (tests and logs key off it):
      ``synced`` / ``unchanged`` / ``skipped:not-a-repo`` / ``skipped:rate-limited``
      / ``skipped:dirty`` / ``skipped:in-progress-op`` / ``failed:<step>``.
    Never raises.

    Steps (plan B3):
      1. ``<repo>/.git`` must exist.
      2. rate limit per repo (``min_interval`` s, stamped at attempt start so
         a flapping remote doesn't turn every claim into a 45 s fetch).
      3. skip with WARNING if ``git status --porcelain --untracked-files=no``
         is non-empty or a merge/rebase is in progress — a dirty anchor is an
         operator problem, never something to auto-resolve.
      4. ``git fetch origin main`` (timeout 45 s, ``GIT_TERMINAL_PROMPT=0`` so
         a missing credential fails fast instead of hanging the dispatcher).
         Deliberately NO ``--prune``: pruning ``origin/wt/*`` between the
         reviewer's branch delete and ``kanban_complete`` makes upstream's
         worktree cleanup think commits are unpushed and leak the worktree.
      5. ``git checkout -q -B main origin/main`` (ff or reset; the tree is
         clean so nothing is lost).
      6. log ``[render-kanban-guard] anchor <repo> <old7>..<new7>``.
    """
    run = run or _default_run
    clock = now or time.monotonic
    repo = str(repo)
    try:
        if not (Path(repo) / ".git").exists():
            log.debug("%s anchor %s: no .git, skipping", LOG_PREFIX, repo)
            return "skipped:not-a-repo"

        last = _LAST_SYNC_ATTEMPT.get(repo)
        t = clock()
        if last is not None and (t - last) < min_interval:
            log.debug("%s anchor %s: rate-limited (%.0fs ago)", LOG_PREFIX, repo, t - last)
            return "skipped:rate-limited"
        _LAST_SYNC_ATTEMPT[repo] = t

        status = _git(run, repo, "status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0:
            log.warning("%s anchor %s: git status failed: %s", LOG_PREFIX, repo,
                        (status.stderr or "").strip()[:300])
            return "failed:status"
        if (status.stdout or "").strip():
            log.warning(
                "%s anchor %s has local modifications; NOT syncing main "
                "(new worktrees will be cut from its current HEAD). Clean it up "
                "on the box: git -C %s status",
                LOG_PREFIX, repo, repo,
            )
            return "skipped:dirty"

        gitdir_res = _git(run, repo, "rev-parse", "--git-dir")
        gitdir = (gitdir_res.stdout or "").strip() if gitdir_res.returncode == 0 else ".git"
        gitdir_path = Path(gitdir) if os.path.isabs(gitdir) else Path(repo) / gitdir
        for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
            if (gitdir_path / marker).exists():
                log.warning(
                    "%s anchor %s has a %s in progress; NOT syncing main",
                    LOG_PREFIX, repo, marker,
                )
                return "skipped:in-progress-op"

        old_res = _git(run, repo, "rev-parse", "HEAD")
        old = (old_res.stdout or "").strip() if old_res.returncode == 0 else ""

        fetch = _git(
            run, repo, "fetch", "origin", "main",
            timeout=fetch_timeout, env={"GIT_TERMINAL_PROMPT": "0"},
        )
        if fetch.returncode != 0:
            log.warning("%s anchor %s: git fetch origin main failed (rc=%s): %s",
                        LOG_PREFIX, repo, fetch.returncode,
                        (fetch.stderr or "").strip()[:300])
            return "failed:fetch"

        co = _git(run, repo, "checkout", "-q", "-B", "main", "origin/main")
        if co.returncode != 0:
            log.warning("%s anchor %s: git checkout -B main origin/main failed (rc=%s): %s",
                        LOG_PREFIX, repo, co.returncode,
                        (co.stderr or "").strip()[:300])
            return "failed:checkout"

        new_res = _git(run, repo, "rev-parse", "HEAD")
        new = (new_res.stdout or "").strip() if new_res.returncode == 0 else ""
        old7, new7 = old[:7] or "?", new[:7] or "?"
        if old and new and old == new:
            log.info("%s anchor %s %s..%s (unchanged)", LOG_PREFIX, repo, old7, new7)
            return "unchanged"
        log.info("%s anchor %s %s..%s", LOG_PREFIX, repo, old7, new7)
        return "synced"
    except subprocess.TimeoutExpired as exc:
        log.warning("%s anchor %s: git timed out: %s", LOG_PREFIX, repo, exc)
        return "failed:timeout"
    except Exception as exc:  # never let the dispatcher see this
        log.warning("%s anchor %s: sync failed: %s", LOG_PREFIX, repo, exc)
        return "failed:exception"


def sync_anchors(
    repos: Iterable[str],
    *,
    run: Optional[RunFn] = None,
    environ: Mapping[str, str] = os.environ,
) -> Dict[str, str]:
    """Sync every anchor unless we are inside a worker process. Never raises."""
    results: Dict[str, str] = {}
    if _in_kanban_worker(environ):
        return results
    for repo in repos:
        try:
            results[repo] = sync_anchor(repo, run=run)
        except Exception as exc:  # pragma: no cover - sync_anchor already guards
            logger.warning("%s anchor %s: unexpected error: %s", LOG_PREFIX, repo, exc)
            results[repo] = "failed:exception"
    return results


# ---------------------------------------------------------------------------
# 3. Role prompt section (pure)
# ---------------------------------------------------------------------------

WORKER_SECTION = (
    "MANDATORY FIRST STEP: call skill_view(\"kanban-sdlc-worker\") and follow it "
    "exactly; it is the house SDLC protocol for this card and overrides any "
    "generic git advice in the repo's CLAUDE.md. Key rules: your branch "
    "(wt/<task>, $HERMES_KANBAN_BRANCH) already exists in "
    "$HERMES_KANBAN_WORKSPACE; \"main\" always means origin/main (fetch it, "
    "never trust a local main); never push to main or master; never use "
    "--no-verify, --force or -f (only --force-with-lease on your own wt/* "
    "branch); never merge anything into main and never run gh pr merge; run the "
    "full GATE before pushing; push wt/<task> and open or refresh the PR with "
    "gh. This run MUST end with exactly one of "
    "kanban_request_review(reviewer=\"reviewer\", summary=..., metadata={pr_url, "
    "head_sha, ...}) or kanban_block(kind=..., reason=...). Printing a summary "
    "and stopping is a protocol violation: the card bounces and the run counts "
    "as a failure."
)

REVIEWER_SECTION = (
    "MANDATORY FIRST STEP: call skill_view(\"kanban-sdlc-reviewer\") and follow "
    "it; it composes with the upstream sdlc-review skill, which is already "
    "preloaded for this review run. You are the merger for this board: verify "
    "the PR head matches the review_requested metadata, run the full GATE "
    "yourself, wait for CI with gh pr checks, then squash-merge with gh pr merge. "
    "Never edit implementation files, never push to main directly, never "
    "--force or --no-verify, never merge red. This run MUST end with exactly one "
    "of kanban_complete(...), kanban_request_changes(...) or kanban_block(...)."
)


def role_prompt_section(
    session_info: Optional[Mapping[str, Any]] = None,
    *,
    environ: Mapping[str, str] = os.environ,
    worker_profiles: Sequence[str] = DEFAULT_WORKER_PROFILES,
    reviewer_profiles: Sequence[str] = DEFAULT_REVIEWER_PROFILES,
) -> str:
    """Text for the ``render-kanban-sdlc-role`` section, or ``""``.

    Empty outside a kanban worker (the gateway, interactive CLI, cron jobs
    all see nothing) and for profiles that are neither worker nor reviewer.
    """
    if not _in_kanban_worker(environ):
        return ""
    profile = _current_profile(environ, session_info)
    if profile in set(worker_profiles):
        return WORKER_SECTION
    if profile in set(reviewer_profiles):
        return REVIEWER_SECTION
    return ""


# Keep the texts inside the registered budget at import time: a too-long
# section is silently skipped by the manager (plugins.py:5370-5379), which
# would be a quiet regression.
assert len(WORKER_SECTION) <= SYSTEM_PROMPT_SECTION_MAX_CHARS, "worker section over budget"
assert len(REVIEWER_SECTION) <= SYSTEM_PROMPT_SECTION_MAX_CHARS, "reviewer section over budget"


# ---------------------------------------------------------------------------
# Hook handlers (thin wrappers over the pure functions; never raise)
# ---------------------------------------------------------------------------


def on_kanban_task_claimed(task_id: str = "", assignee: Any = None, **_: Any) -> None:
    """``kanban_task_claimed`` observer (return value ignored, plugins.py:259)."""
    try:
        if _in_kanban_worker():
            return None
        results = sync_anchors(_SETTINGS.anchor_repos)
        logger.debug("%s claimed %s (assignee=%s): anchor sync %s",
                     LOG_PREFIX, task_id, assignee, results)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("%s kanban_task_claimed handler failed: %s", LOG_PREFIX, exc)
    return None


def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    """``pre_tool_call`` handler: block dangerous git/gh commands in workers."""
    try:
        if not _in_kanban_worker():
            return None
        arg_key = COMMAND_TOOLS.get(tool_name)
        if arg_key is None or not isinstance(args, Mapping):
            return None
        text = args.get(arg_key)
        if not isinstance(text, str):
            return None
        reason = classify_command(
            text,
            profile=_current_profile(),
            tool_name=tool_name,
            anchor_repos=_SETTINGS.anchor_repos,
            worker_profiles=_SETTINGS.worker_profiles,
            reviewer_profiles=_SETTINGS.reviewer_profiles,
            protected_branches=_SETTINGS.protected_branches,
        )
        if reason is None:
            return None
        logger.warning("%s blocked %s call in task %s: %s", LOG_PREFIX, tool_name,
                       os.environ.get("HERMES_KANBAN_TASK", "?"), reason[:120])
        return {"action": "block", "message": f"{LOG_PREFIX} blocked: {reason}"}
    except Exception as exc:  # pragma: no cover - defensive; fail OPEN
        logger.warning("%s pre_tool_call handler failed: %s", LOG_PREFIX, exc)
        return None


def _prompt_section_provider(session_info: Mapping[str, Any]) -> str:
    try:
        return role_prompt_section(
            session_info,
            worker_profiles=_SETTINGS.worker_profiles,
            reviewer_profiles=_SETTINGS.reviewer_profiles,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("%s prompt section failed: %s", LOG_PREFIX, exc)
        return ""


def register(ctx: Any) -> None:
    """Plugin entry point (plugins.py:4788-4795 calls ``register(ctx)``)."""
    global _SETTINGS
    _SETTINGS = settings_from_ctx(ctx)
    # register_hook(name, fn): plugins.py:3109-3132 (unknown names only warn).
    ctx.register_hook("kanban_task_claimed", on_kanban_task_claimed)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    # register_system_prompt_section(id, content, *, position, max_chars):
    # plugins.py:3134-3141; callable receives the frozen session-info mapping.
    try:
        ctx.register_system_prompt_section(
            SYSTEM_PROMPT_SECTION_ID,
            _prompt_section_provider,
            max_chars=SYSTEM_PROMPT_SECTION_MAX_CHARS,
        )
    except Exception as exc:  # e.g. duplicate id on a hot reload; keep the hooks
        logger.warning("%s could not register prompt section: %s", LOG_PREFIX, exc)
    logger.info(
        "%s registered (anchors=%s workers=%s reviewers=%s)",
        LOG_PREFIX, _SETTINGS.anchor_repos, _SETTINGS.worker_profiles,
        _SETTINGS.reviewer_profiles,
    )
