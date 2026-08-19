"""Tests for scripts/patch-config.py (root + profile modes, tier semantics).

The patcher imports PyYAML (which ships in Hermes' .venv, the interpreter in
its shebang). On a dev machine without PyYAML the dict-level tests still run
against a stub `yaml` module; the file round-trip tests are skipped rather
than failed so `python3 -m unittest` stays green everywhere, and in the image
(`python -m unittest discover -s /tests`) everything runs for real.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    import yaml  # type: ignore

    HAVE_YAML = True
except ImportError:  # pragma: no cover - depends on the host
    yaml = None  # type: ignore
    HAVE_YAML = False


def load_patch_config():
    module_path = REPO_ROOT / "scripts" / "patch-config.py"
    spec = importlib.util.spec_from_file_location("patch_config", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    if not HAVE_YAML:
        # Enough of a stub for the module to import; only the dict-level
        # helpers are exercised without real PyYAML.
        sys.modules.setdefault("yaml", types.SimpleNamespace(YAMLError=Exception))
    spec.loader.exec_module(module)
    return module


def run_main(module, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as exc:  # load_config exits 0 on parse failure
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


# ----------------------------------------------------------- dict-level tests

class HelperTests(unittest.TestCase):
    def setUp(self):
        self.pc = load_patch_config()

    def test_default_render_mcp_entry_does_not_filter_tools(self):
        self.assertNotIn("tools", self.pc._render_entry())

    def test_ensure_nested_inserts_when_missing(self):
        cfg: dict = {}
        self.assertTrue(self.pc.ensure_nested(cfg, "kanban.max_in_progress", 2))
        self.assertEqual(cfg, {"kanban": {"max_in_progress": 2}})
        # Second call: nothing to do.
        self.assertFalse(self.pc.ensure_nested(cfg, "kanban.max_in_progress", 2))

    def test_ensure_nested_insert_only_keeps_existing_value(self):
        cfg = {"kanban": {"failure_limit": 5}}
        self.assertFalse(self.pc.ensure_nested(cfg, "kanban.failure_limit", 2))
        self.assertEqual(cfg["kanban"]["failure_limit"], 5)
        # An explicit empty string is a value the user owns.
        cfg = {"auxiliary": {"kanban_decomposer": {"model": ""}}}
        self.assertFalse(self.pc.ensure_nested(cfg, "auxiliary.kanban_decomposer.model", "x"))
        self.assertEqual(cfg["auxiliary"]["kanban_decomposer"]["model"], "")

    def test_ensure_nested_treats_null_leaf_as_absent(self):
        cfg = {"kanban": {"failure_limit": None}}
        self.assertTrue(self.pc.ensure_nested(cfg, "kanban.failure_limit", 2))
        self.assertEqual(cfg["kanban"]["failure_limit"], 2)
        cfg = {"kanban": None}
        self.assertTrue(self.pc.ensure_nested(cfg, "kanban.failure_limit", 2))
        self.assertEqual(cfg, {"kanban": {"failure_limit": 2}})

    def test_ensure_nested_enforce_overwrites_and_is_type_strict(self):
        cfg = {"kanban": {"max_in_progress": 3}}
        self.assertTrue(self.pc.ensure_nested(cfg, "kanban.max_in_progress", 2, enforce=True))
        self.assertEqual(cfg["kanban"]["max_in_progress"], 2)
        self.assertFalse(self.pc.ensure_nested(cfg, "kanban.max_in_progress", 2, enforce=True))
        # `True == 1` in Python; an enforced int must still replace a bool.
        cfg = {"kanban": {"max_in_progress_per_profile": True}}
        self.assertTrue(
            self.pc.ensure_nested(cfg, "kanban.max_in_progress_per_profile", 1, enforce=True)
        )
        self.assertIs(cfg["kanban"]["max_in_progress_per_profile"], 1)

    def test_ensure_nested_never_clobbers_non_mapping_intermediate(self):
        cfg = {"kanban": True}
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(self.pc.ensure_nested(cfg, "kanban.max_in_progress", 2, enforce=True))
        self.assertEqual(cfg, {"kanban": True})
        self.assertIn("not a mapping", err.getvalue())

    def test_ensure_nested_copies_mutable_values(self):
        value = ["/opt/data/work/stopsargassum"]
        cfg: dict = {}
        self.pc.ensure_nested(cfg, "plugins.entries.g.settings.anchor_repos", value)
        cfg["plugins"]["entries"]["g"]["settings"]["anchor_repos"].append("x")
        self.assertEqual(value, ["/opt/data/work/stopsargassum"])

    def test_ensure_list_contains_creates_appends_and_dedupes(self):
        cfg: dict = {}
        self.assertTrue(self.pc.ensure_list_contains(cfg, "plugins.enabled", "a"))
        self.assertEqual(cfg, {"plugins": {"enabled": ["a"]}})
        self.assertTrue(self.pc.ensure_list_contains(cfg, "plugins.enabled", "b"))
        self.assertFalse(self.pc.ensure_list_contains(cfg, "plugins.enabled", "a"))
        self.assertEqual(cfg["plugins"]["enabled"], ["a", "b"])

    def test_ensure_list_contains_skips_non_list(self):
        cfg = {"plugins": {"enabled": "a"}}
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(self.pc.ensure_list_contains(cfg, "plugins.enabled", "b"))
        self.assertEqual(cfg["plugins"]["enabled"], "a")
        self.assertIn("not a list", err.getvalue())

    def test_root_tiers_are_disjoint_and_cover_plan_b1(self):
        enforced = set(self.pc.ROOT_ENFORCED)
        insert = set(self.pc.ROOT_INSERT_ONLY)
        self.assertFalse(enforced & insert)
        self.assertEqual(
            enforced,
            {
                "kanban.dispatch_in_gateway",
                "kanban.orchestrator_profile",
                "kanban.default_assignee",
                "kanban.max_in_progress",
                "kanban.max_in_progress_per_profile",
                "kanban.auto_decompose",
            },
        )
        for key in (
            "kanban.review_dispatch",
            "kanban.dispatch_interval_seconds",
            "kanban.failure_limit",
            "kanban.auto_decompose_per_tick",
            "kanban.dispatch_stale_timeout_seconds",
            "kanban.reconcile_orphans",
            "auxiliary.kanban_decomposer.provider",
            "auxiliary.kanban_decomposer.model",
            "auxiliary.triage_specifier.provider",
            "auxiliary.triage_specifier.model",
            "delegation.model",
            "delegation.provider",
            "plugins.entries.render-kanban-guard.settings.anchor_repos",
            "plugins.entries.render-kanban-guard.settings.worker_profiles",
            "plugins.entries.render-kanban-guard.settings.reviewer_profiles",
        ):
            self.assertIn(key, insert)
        self.assertEqual(self.pc.ROOT_LIST_APPEND, {"plugins.enabled": ["render-kanban-guard"]})
        self.assertEqual(self.pc.ROOT_ENFORCED["kanban.default_assignee"], "coder")
        self.assertEqual(self.pc.ROOT_ENFORCED["kanban.orchestrator_profile"], "default")
        self.assertEqual(self.pc.ROOT_ENFORCED["kanban.max_in_progress"], 2)
        self.assertEqual(self.pc.ROOT_ENFORCED["kanban.max_in_progress_per_profile"], 1)
        self.assertIs(self.pc.ROOT_ENFORCED["kanban.auto_decompose"], False)
        self.assertIs(self.pc.ROOT_ENFORCED["kanban.dispatch_in_gateway"], True)

    def test_profile_models_match_target_architecture(self):
        self.assertEqual(
            self.pc.PROFILE_MODELS["coder"], ("deepseek/deepseek-v4-pro-0813", "openrouter")
        )
        self.assertEqual(self.pc.PROFILE_MODELS["reviewer"], ("openai/gpt-5.6-sol", "openrouter"))
        self.assertEqual(
            self.pc.profile_enforced("reviewer"),
            {
                "model.default": "openai/gpt-5.6-sol",
                "model.provider": "openrouter",
                # Every non-root profile is pinned to never run the dispatcher.
                "kanban.dispatch_in_gateway": False,
            },
        )

    def test_other_profile_mode_only_pins_dispatcher(self):
        # 'other' = a profile we do not own (engine-research, claude, ...):
        # only kanban.dispatch_in_gateway=false is enforced; model, skills,
        # plugins and anything else the operator set are left alone.
        self.assertEqual(self.pc.profile_enforced("other"), {"kanban.dispatch_in_gateway": False})
        cfg = {
            "model": {"default": "x/y", "provider": "p"},
            "kanban": {"dispatch_in_gateway": True, "max_in_progress": 7},
            "mcp_servers": {"foo": {}},
            "plugins": {"enabled": ["something"]},
        }
        report = self.pc.patch_profile(cfg, "other")
        self.assertTrue(report.changed)
        self.assertFalse(cfg["kanban"]["dispatch_in_gateway"])
        self.assertEqual(cfg["kanban"]["max_in_progress"], 7)
        self.assertEqual(cfg["model"], {"default": "x/y", "provider": "p"})
        self.assertEqual(cfg["mcp_servers"], {"foo": {}})
        self.assertEqual(cfg["plugins"], {"enabled": ["something"]})
        # Second pass is a no-op.
        self.assertFalse(self.pc.patch_profile(cfg, "other").changed)

    def test_patch_root_dict_semantics(self):
        cfg = {
            "kanban": {"max_in_progress": 4, "failure_limit": 9},
            "plugins": {"enabled": ["kanban"]},
        }
        report = self.pc.patch_root(cfg)
        self.assertTrue(report.changed)
        self.assertEqual(cfg["kanban"]["max_in_progress"], 2)  # enforced
        self.assertEqual(cfg["kanban"]["failure_limit"], 9)  # insert-only, user wins
        self.assertEqual(cfg["plugins"]["enabled"], ["kanban", "render-kanban-guard"])
        self.assertEqual(
            cfg["plugins"]["entries"]["render-kanban-guard"]["settings"],
            {
                "anchor_repos": ["/opt/data/work/stopsargassum"],
                "worker_profiles": ["coder"],
                "reviewer_profiles": ["reviewer"],
            },
        )
        self.assertIn("mcp_servers.render", report.inserted)
        self.assertTrue(any(e.startswith("kanban.max_in_progress (") for e in report.enforced))
        # Idempotent at the dict level too.
        self.assertFalse(self.pc.patch_root(cfg).changed)

    def test_patch_profile_promotes_bare_model_shorthand(self):
        cfg = {"model": "old/thing"}
        report = self.pc.patch_profile(cfg, "coder")
        self.assertEqual(
            cfg["model"], {"default": "deepseek/deepseek-v4-pro-0813", "provider": "openrouter"}
        )
        self.assertTrue(any(e.startswith("model.default (") for e in report.enforced))


# ------------------------------------------------------- file round-trip tests

@unittest.skipUnless(HAVE_YAML, "PyYAML not importable on this host; run inside the image")
class RootFileTests(unittest.TestCase):
    def setUp(self):
        self.pc = load_patch_config()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "config.yaml"

    def read(self, path: Path | None = None) -> dict:
        return yaml.safe_load((path or self.path).read_text(encoding="utf-8"))

    def test_fresh_root_config_gets_everything(self):
        code, out, _ = run_main(self.pc, [str(self.path)])
        self.assertEqual(code, 0)
        cfg = self.read()
        self.assertEqual(cfg["mcp_servers"]["render"]["url"], self.pc.RENDER_MCP_URL)
        self.assertEqual(cfg["skills"]["external_dirs"], list(self.pc.RENDER_SKILL_DIRS))
        self.assertEqual(cfg["kanban"]["default_assignee"], "coder")
        self.assertEqual(cfg["kanban"]["max_in_progress"], 2)
        self.assertEqual(cfg["kanban"]["max_in_progress_per_profile"], 1)
        self.assertIs(cfg["kanban"]["auto_decompose"], False)
        self.assertIs(cfg["kanban"]["dispatch_in_gateway"], True)
        self.assertEqual(cfg["kanban"]["dispatch_stale_timeout_seconds"], 14400)
        self.assertEqual(
            cfg["auxiliary"]["kanban_decomposer"],
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash-0731"},
        )
        self.assertEqual(
            cfg["auxiliary"]["triage_specifier"],
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash-0731"},
        )
        self.assertEqual(
            cfg["delegation"], {"model": "deepseek/deepseek-v4-pro-0813", "provider": "openrouter"}
        )
        self.assertEqual(cfg["plugins"]["enabled"], ["render-kanban-guard"])
        self.assertEqual(
            cfg["plugins"]["entries"]["render-kanban-guard"]["settings"]["anchor_repos"],
            ["/opt/data/work/stopsargassum"],
        )
        self.assertEqual(out.count("\n"), 1)  # exactly one summary line
        self.assertIn("patched", out)
        self.assertIn("(root)", out)
        self.assertIn("inserted", out)
        self.assertNotIn("enforced", out)  # first write: nothing to overwrite

    def test_second_run_is_a_no_write(self):
        self.assertEqual(run_main(self.pc, [str(self.path)])[0], 0)
        before_text = self.path.read_text(encoding="utf-8")
        before_stat = self.path.stat()
        code, out, err = run_main(self.pc, [str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before_text)
        self.assertEqual(self.path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertFalse(list(self.dir.glob("*.tmp")))  # temp file never left behind

    def test_insert_only_keys_preserve_user_values_enforced_keys_do_not(self):
        self.path.write_text(
            "kanban:\n"
            "  max_in_progress: 5\n"
            "  auto_decompose: true\n"
            "  failure_limit: 7\n"
            "  review_dispatch: false\n"
            "auxiliary:\n"
            "  kanban_decomposer:\n"
            "    model: someone/else\n"
            "delegation:\n"
            "  model: custom/model\n"
            "mcp_servers:\n"
            "  render:\n"
            "    url: https://example.invalid/mcp\n"
            "skills:\n"
            "  external_dirs: [/home/me/skills]\n"
            "plugins:\n"
            "  enabled: [kanban, render-kanban-guard]\n"
            "  entries:\n"
            "    render-kanban-guard:\n"
            "      settings:\n"
            "        anchor_repos: [/srv/other]\n"
            "unrelated:\n"
            "  keep: me\n",
            encoding="utf-8",
        )
        code, out, _ = run_main(self.pc, [str(self.path)])
        self.assertEqual(code, 0)
        cfg = self.read()
        # Enforced -> overwritten and logged with old -> new.
        self.assertEqual(cfg["kanban"]["max_in_progress"], 2)
        self.assertIs(cfg["kanban"]["auto_decompose"], False)
        self.assertIn("enforced", out)
        self.assertIn("kanban.max_in_progress (5 -> 2)", out)
        self.assertIn("kanban.auto_decompose (true -> false)", out)
        # Insert-only -> user wins.
        self.assertEqual(cfg["kanban"]["failure_limit"], 7)
        self.assertIs(cfg["kanban"]["review_dispatch"], False)
        self.assertEqual(cfg["auxiliary"]["kanban_decomposer"]["model"], "someone/else")
        self.assertEqual(cfg["auxiliary"]["kanban_decomposer"]["provider"], "openrouter")
        self.assertEqual(cfg["delegation"]["model"], "custom/model")
        self.assertEqual(cfg["delegation"]["provider"], "openrouter")
        self.assertEqual(cfg["mcp_servers"]["render"]["url"], "https://example.invalid/mcp")
        self.assertEqual(
            cfg["plugins"]["entries"]["render-kanban-guard"]["settings"]["anchor_repos"],
            ["/srv/other"],
        )
        self.assertEqual(
            cfg["plugins"]["entries"]["render-kanban-guard"]["settings"]["worker_profiles"],
            ["coder"],
        )
        # Lists: appended, never reordered or duplicated.
        self.assertEqual(
            cfg["skills"]["external_dirs"],
            ["/home/me/skills", *self.pc.RENDER_SKILL_DIRS],
        )
        self.assertEqual(cfg["plugins"]["enabled"], ["kanban", "render-kanban-guard"])
        # Untouched sections survive.
        self.assertEqual(cfg["unrelated"], {"keep": "me"})
        # Second run: stable.
        self.assertIn("nothing to do", run_main(self.pc, [str(self.path)])[1])

    def test_plugins_enabled_append_without_duplicates(self):
        self.path.write_text("plugins:\n  enabled: [render-kanban-guard]\n", encoding="utf-8")
        run_main(self.pc, [str(self.path)])
        self.assertEqual(self.read()["plugins"]["enabled"], ["render-kanban-guard"])
        self.path.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
        run_main(self.pc, [str(self.path)])
        self.assertEqual(self.read()["plugins"]["enabled"], ["render-kanban-guard"])

    def test_parse_failure_exits_zero_and_leaves_file_untouched(self):
        broken = "kanban: [\n  this is not yaml\n"
        self.path.write_text(broken, encoding="utf-8")
        code, out, err = run_main(self.pc, [str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)
        self.assertIn("not valid YAML", err)
        self.assertEqual(out, "")
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_non_mapping_intermediate_is_skipped_not_clobbered(self):
        self.path.write_text("kanban: true\nplugins: nope\n", encoding="utf-8")
        code, _, err = run_main(self.pc, [str(self.path)])
        self.assertEqual(code, 0)
        cfg = self.read()
        self.assertIs(cfg["kanban"], True)
        self.assertEqual(cfg["plugins"], "nope")
        self.assertIn("not a mapping", err)
        # Everything else still lands.
        self.assertEqual(cfg["delegation"]["provider"], "openrouter")

    def test_file_mode_is_preserved_on_rewrite(self):
        self.path.write_text("kanban: {}\n", encoding="utf-8")
        self.path.chmod(0o640)
        run_main(self.pc, [str(self.path)])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)


@unittest.skipUnless(HAVE_YAML, "PyYAML not importable on this host; run inside the image")
class ProfileFileTests(unittest.TestCase):
    def setUp(self):
        self.pc = load_patch_config()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profiles = Path(self._tmp.name) / "profiles"

    def cfg_path(self, profile: str) -> Path:
        return self.profiles / profile / "config.yaml"

    def read(self, profile: str) -> dict:
        return yaml.safe_load(self.cfg_path(profile).read_text(encoding="utf-8"))

    def assert_profile_shape(self, profile: str, model: str):
        cfg = self.read(profile)
        self.assertEqual(cfg["model"], {"default": model, "provider": "openrouter"})
        self.assertEqual(cfg["skills"]["external_dirs"], ["/opt/render-tools/skills-local"])
        self.assertEqual(cfg["plugins"]["enabled"], ["render-kanban-guard"])
        self.assertEqual(
            cfg["plugins"]["entries"]["render-kanban-guard"]["settings"],
            {
                "anchor_repos": ["/opt/data/work/stopsargassum"],
                "worker_profiles": ["coder"],
                "reviewer_profiles": ["reviewer"],
            },
        )
        # Profiles get no Render MCP server (smaller surface for workers).
        self.assertNotIn("mcp_servers", cfg)

    def test_coder_profile_explicit_flag(self):
        path = self.cfg_path("coder")
        code, out, _ = run_main(self.pc, ["--profile-config", str(path), "--profile", "coder"])
        self.assertEqual(code, 0)
        self.assertIn("(profile coder)", out)
        self.assert_profile_shape("coder", "deepseek/deepseek-v4-pro-0813")
        # Idempotent: second run writes nothing.
        before = path.stat().st_mtime_ns
        code, out, _ = run_main(self.pc, ["--profile-config", str(path), "--profile", "coder"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_reviewer_profile_inferred_from_directory(self):
        path = self.cfg_path("reviewer")
        code, out, _ = run_main(self.pc, ["--profile-config", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("(profile reviewer)", out)
        self.assert_profile_shape("reviewer", "openai/gpt-5.6-sol")

    def test_profile_model_is_enforced_but_other_keys_are_not(self):
        path = self.cfg_path("coder")
        path.parent.mkdir(parents=True)
        path.write_text(
            "model:\n"
            "  default: anthropic/claude-opus-4.6\n"
            "  provider: anthropic\n"
            "  reasoning_effort: high\n"
            "skills:\n"
            "  external_dirs: [/opt/render-tools/skills-local, /extra]\n"
            "plugins:\n"
            "  enabled: [foo, render-kanban-guard]\n"
            "  entries:\n"
            "    render-kanban-guard:\n"
            "      settings:\n"
            "        worker_profiles: [coder, other]\n"
            "terminal:\n"
            "  timeout: 900\n",
            encoding="utf-8",
        )
        code, out, _ = run_main(self.pc, ["--profile-config", str(path), "--profile", "coder"])
        self.assertEqual(code, 0)
        cfg = self.read("coder")
        self.assertEqual(cfg["model"]["default"], "deepseek/deepseek-v4-pro-0813")
        self.assertEqual(cfg["model"]["provider"], "openrouter")
        self.assertEqual(cfg["model"]["reasoning_effort"], "high")  # sibling untouched
        self.assertIn("model.default (anthropic/claude-opus-4.6 -> deepseek/deepseek-v4-pro-0813)", out)
        self.assertIn("model.provider (anthropic -> openrouter)", out)
        self.assertEqual(cfg["skills"]["external_dirs"], ["/opt/render-tools/skills-local", "/extra"])
        self.assertEqual(cfg["plugins"]["enabled"], ["foo", "render-kanban-guard"])
        settings = cfg["plugins"]["entries"]["render-kanban-guard"]["settings"]
        self.assertEqual(settings["worker_profiles"], ["coder", "other"])  # user wins
        self.assertEqual(settings["anchor_repos"], ["/opt/data/work/stopsargassum"])  # filled in
        self.assertEqual(cfg["terminal"], {"timeout": 900})

    def test_bare_model_shorthand_is_promoted(self):
        path = self.cfg_path("reviewer")
        path.parent.mkdir(parents=True)
        path.write_text("model: some/old-model\n", encoding="utf-8")
        code, out, _ = run_main(self.pc, ["--profile-config", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read("reviewer")["model"],
            {"default": "openai/gpt-5.6-sol", "provider": "openrouter"},
        )
        self.assertIn("model.default (some/old-model -> openai/gpt-5.6-sol)", out)

    def test_unknown_profile_exits_2_with_usage(self):
        path = self.cfg_path("coder")
        code, _, err = run_main(self.pc, ["--profile-config", str(path), "--profile", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)
        self.assertIn("unknown profile 'nope'", err)
        self.assertFalse(path.exists())
        # Un-inferable path without --profile: also 2, also nothing written.
        other = self.profiles / "whatever.yaml"
        code, _, err = run_main(self.pc, ["--profile-config", str(other)])
        self.assertEqual(code, 2)
        self.assertFalse(other.exists())

    def test_profile_parse_failure_exits_zero_untouched(self):
        path = self.cfg_path("coder")
        path.parent.mkdir(parents=True)
        broken = "model: {\n"
        path.write_text(broken, encoding="utf-8")
        code, _, err = run_main(self.pc, ["--profile-config", str(path), "--profile", "coder"])
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), broken)
        self.assertIn("not valid YAML", err)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.pc = load_patch_config()

    def test_no_args_exits_2(self):
        code, _, err = run_main(self.pc, [])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_root_and_profile_together_exits_2(self):
        code, _, err = run_main(self.pc, ["a.yaml", "--profile-config", "b.yaml"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_profile_flag_without_profile_config_exits_2(self):
        code, _, err = run_main(self.pc, ["a.yaml", "--profile", "coder"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)


if __name__ == "__main__":
    unittest.main()
