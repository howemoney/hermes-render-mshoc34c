"""Tests for plugins/render-kanban-guard (no Hermes runtime required).

The plugin module is loaded straight from ``plugins/render-kanban-guard/
__init__.py`` via importlib, exactly the way Hermes' PluginManager does for a
directory plugin (``_load_directory_module``, hermes_cli/plugins.py:4954),
then ``register()`` is driven with a stub ctx that records what was
registered. The pure functions (command classifier, anchor sync with an
injectable runner, prompt section) are exercised directly.

Anchor-sync tests use REAL git against a temp bare remote so the exact
``git fetch origin main`` + ``git checkout -q -B main origin/main`` sequence
is proven to move the anchor's main, not just that the right strings were
passed. A recording runner additionally pins the contract the plan cares
about: no ``--prune``, ``GIT_TERMINAL_PROMPT=0``, 45 s fetch timeout.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "render-kanban-guard"

ANCHOR = "/opt/data/work/stopsargassum"


def load_plugin():
    module_path = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location("render_kanban_guard_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StubCtx:
    """Minimal stand-in for hermes_cli.plugins.PluginContext."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.hooks = {}
        self.sections = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_system_prompt_section(self, section_id, content, *, position="after_memory", max_chars=4000):
        if section_id in self.sections:
            raise ValueError("duplicate")
        self.sections[section_id] = (content, position, max_chars)


def git(*args, cwd, env=None):
    merged = dict(os.environ)
    merged.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if env:
        merged.update(env)
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=merged,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Manifest + registration
# ---------------------------------------------------------------------------


class ManifestTests(unittest.TestCase):
    def test_manifest_declares_expected_fields(self):
        text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: render-kanban-guard", text)
        self.assertIn('version: "0.1.0"', text)
        self.assertIn("kind: standalone", text)
        self.assertIn('author: "Howe Agency"', text)
        self.assertIn("  - kanban_task_claimed", text)
        self.assertIn("  - pre_tool_call", text)
        # PyYAML is optional on the host; when present, prove it parses.
        try:
            import yaml  # type: ignore
        except Exception:  # pragma: no cover
            return
        data = yaml.safe_load(text)
        self.assertEqual(data["name"], "render-kanban-guard")
        self.assertEqual(data["kind"], "standalone")
        self.assertEqual(data["hooks"], ["kanban_task_claimed", "pre_tool_call"])
        self.assertEqual(data["provides_hooks"], data["hooks"])
        self.assertEqual(data["config_schema"]["anchor_repos"]["default"], [ANCHOR])
        self.assertEqual(data["config_schema"]["worker_profiles"]["default"], ["coder"])
        self.assertEqual(data["config_schema"]["reviewer_profiles"]["default"], ["reviewer"])
        # Every top-level key must be one Hermes v2026.8.18 knows about
        # (hermes_cli/plugins.py:653-662); with manifest_version 2 unknown
        # keys log a WARNING at every boot.
        known = {
            "name", "version", "description", "author", "requires_env",
            "provides_tools", "provides_hooks", "kind", "hooks", "label",
            "optional_env", "platforms", "external_dependencies", "pip_dependencies",
            "provides_browser_providers", "provides_web_providers",
            "manifest_version", "api_version", "requires_plugins",
            "python_dependencies", "config_schema", "license", "homepage", "tags",
            "capabilities", "emits", "listens", "hermes", "depends",
        }
        self.assertEqual(set(data) - known, set())


class RegisterTests(unittest.TestCase):
    def test_register_wires_hooks_and_section_with_defaults(self):
        mod = load_plugin()
        ctx = StubCtx()
        mod.register(ctx)
        self.assertEqual(ctx.hooks["kanban_task_claimed"], [mod.on_kanban_task_claimed])
        self.assertEqual(ctx.hooks["pre_tool_call"], [mod.on_pre_tool_call])
        self.assertIn("render-kanban-sdlc-role", ctx.sections)
        _content, _pos, max_chars = ctx.sections["render-kanban-sdlc-role"]
        self.assertEqual(max_chars, 1500)
        self.assertEqual(mod._SETTINGS.anchor_repos, [ANCHOR])
        self.assertEqual(mod._SETTINGS.worker_profiles, ["coder"])
        self.assertEqual(mod._SETTINGS.reviewer_profiles, ["reviewer"])

    def test_register_reads_settings_from_ctx(self):
        mod = load_plugin()
        ctx = StubCtx({
            "anchor_repos": ["/srv/a", "/srv/b"],
            "worker_profiles": ["coder", "coder2"],
            "reviewer_profiles": "rev",  # scalar tolerated
        })
        mod.register(ctx)
        self.assertEqual(mod._SETTINGS.anchor_repos, ["/srv/a", "/srv/b"])
        self.assertEqual(mod._SETTINGS.worker_profiles, ["coder", "coder2"])
        self.assertEqual(mod._SETTINGS.reviewer_profiles, ["rev"])

    def test_register_survives_duplicate_section_registration(self):
        mod = load_plugin()
        ctx = StubCtx()
        ctx.sections["render-kanban-sdlc-role"] = ("x", "after_memory", 1)
        mod.register(ctx)  # must not raise; hooks still wired
        self.assertIn("pre_tool_call", ctx.hooks)

    def test_section_texts_fit_budget(self):
        mod = load_plugin()
        self.assertLessEqual(len(mod.WORKER_SECTION), 1500)
        self.assertLessEqual(len(mod.REVIEWER_SECTION), 1500)
        self.assertGreater(len(mod.WORKER_SECTION), 600)


# ---------------------------------------------------------------------------
# (a) Command classifier allow/block table
# ---------------------------------------------------------------------------


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()

    def classify(self, cmd, profile="coder", tool_name="terminal"):
        return self.mod.classify_command(cmd, profile=profile, tool_name=tool_name)

    def test_allow_table(self):
        allowed = [
            "git push -u origin wt/t_abc123",
            "git push origin wt/t_abc123",
            "git push",
            "git push origin HEAD",
            "git push origin HEAD:refs/heads/wt/t_1",
            "git push --force-with-lease origin wt/t_1",
            "git push --force-with-lease=wt/t_1:abc origin wt/t_1",
            "git push --force-with-lease --force-if-includes origin wt/t_1",
            "git push -o ci.skip origin wt/t_1",
            "git fetch origin main && git merge --no-edit origin/main",
            "git log --oneline origin/main -5",
            "git checkout -b feature main",
            "git -C /opt/data/work/stopsargassum/.worktrees/t_1 push -u origin wt/t_1",
            "cd /opt/data/work/stopsargassum/.worktrees/t_1 && git push -u origin wt/t_1",
            "gh pr create --base main --head wt/t_1 --title x --body-file /tmp/b.md",
            "gh pr view 12 --json mergeable,mergeStateStatus",
            "gh pr checks 12 --watch",
            "gh api repos/howemoney/stopsargassum/pulls/12",
            "npm test && git status",
            "echo 'never git push origin main'"[:4],  # plain echo
            "",
        ]
        for cmd in allowed:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.classify(cmd))

    def test_block_push_to_protected(self):
        blocked = [
            "git push origin main",
            "git push origin master",
            "git push origin HEAD:main",
            "git push origin HEAD:refs/heads/main",
            "git push -u origin main",
            "git push origin wt/t_1:main",
            "git push origin :main",
            "git push origin --delete main",
            "git push origin -d main",
            "git push origin Main",
            "GIT_TERMINAL_PROMPT=0 git push origin main",
            "/usr/bin/git push origin main",
            "git push 'origin' \"main\"",
            "git   push    origin     main",
            "git push \\\n  origin \\\n  main",
            "git fetch origin && git push origin main",
            "git push origin wt/t_1; git push origin main",
            "git push origin wt/t_1 main",
            "git push --all origin",
            "git push --mirror origin",
            "git push origin +wt/t_1",
            "git -c user.name=x push origin main",
            "git --no-pager push origin main",
        ]
        for cmd in blocked:
            with self.subTest(cmd=cmd):
                reason = self.classify(cmd)
                self.assertIsNotNone(reason, cmd)
                self.assertIn("not allowed", reason)

    def test_block_force_and_no_verify(self):
        for cmd in (
            "git push --force origin wt/t_1",
            "git push -f origin wt/t_1",
            "git push -fu origin wt/t_1",
            "git push -uf origin wt/t_1",
            "git push origin wt/t_1 --force",
            "git push --no-verify origin wt/t_1",
            "git push origin wt/t_1 --no-verify",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.classify(cmd), cmd)
        # and the reviewer is held to the same push rules
        self.assertIsNotNone(self.classify("git push --force origin wt/t_1", profile="reviewer"))
        self.assertIsNotNone(self.classify("git push origin main", profile="reviewer"))

    def test_block_push_from_anchor(self):
        for cmd in (
            f"git -C {ANCHOR} push origin wt/t_1",
            f"git -C {ANCHOR}/ push",
            f"git -C{ANCHOR} push origin wt/t_1",
            f"cd {ANCHOR} && git push",
            f"cd {ANCHOR}; git push -u origin wt/t_1",
        ):
            with self.subTest(cmd=cmd):
                reason = self.classify(cmd)
                self.assertIsNotNone(reason, cmd)
                self.assertIn("anchor", reason)
        # another repo with a similar prefix is fine
        self.assertIsNone(self.classify(f"git -C {ANCHOR}-other push origin wt/t_1"))

    def test_merge_rules_are_worker_only(self):
        merges = [
            "gh pr merge 12 --squash --delete-branch",
            "gh  pr   merge 12",
            "gh api -X PUT repos/howemoney/stopsargassum/pulls/12/merge -f merge_method=squash",
            "curl -X PUT -H 'Authorization: Bearer $GH_TOKEN' https://api.github.com/repos/o/r/pulls/12/merge",
        ]
        for cmd in merges:
            with self.subTest(cmd=cmd, profile="coder"):
                reason = self.classify(cmd, profile="coder")
                self.assertIsNotNone(reason, cmd)
                self.assertIn("reviewer", reason)
            with self.subTest(cmd=cmd, profile="reviewer"):
                self.assertIsNone(self.classify(cmd, profile="reviewer"), cmd)
            with self.subTest(cmd=cmd, profile=""):
                self.assertIsNone(self.classify(cmd, profile=""), cmd)

    def test_execute_code_python_shapes(self):
        py_blocked = [
            'subprocess.run(["git", "push", "origin", "main"], check=True)',
            "os.system('git push -f origin wt/t_1')",
            'subprocess.check_call("git push --no-verify origin wt/t_1", shell=True)',
            'subprocess.run(["gh", "pr", "merge", "12", "--squash"])',
        ]
        for code in py_blocked:
            with self.subTest(code=code):
                self.assertIsNotNone(self.classify(code, tool_name="execute_code"), code)
        self.assertIsNone(self.classify(
            'subprocess.run(["git", "push", "-u", "origin", "wt/t_1"])', tool_name="execute_code"))
        self.assertIsNone(self.classify(
            'subprocess.run(["gh", "pr", "merge", "12"])', profile="reviewer", tool_name="execute_code"))

    def test_custom_protected_branches(self):
        self.assertIsNotNone(self.mod.classify_command(
            "git push origin release", profile="coder", protected_branches=["release"]))
        self.assertIsNone(self.mod.classify_command(
            "git push origin main", profile="coder", protected_branches=["release"]))


class PreToolCallHookTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.mod.register(StubCtx())

    def test_noop_outside_worker(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_KANBAN_TASK", None)
            self.assertIsNone(self.mod.on_pre_tool_call(
                tool_name="terminal", args={"command": "git push origin main"}))

    def test_blocks_in_worker_and_returns_contract_shape(self):
        env = {"HERMES_KANBAN_TASK": "t_1", "HERMES_PROFILE": "coder"}
        with mock.patch.dict(os.environ, env, clear=False):
            result = self.mod.on_pre_tool_call(
                tool_name="terminal", args={"command": "git push origin main"},
                task_id="t_1", session_id="s", tool_call_id="c", turn_id="", api_request_id="",
                middleware_trace=[], telemetry_schema_version=1,
            )
            self.assertIsInstance(result, dict)
            self.assertEqual(result["action"], "block")
            self.assertTrue(result["message"].startswith("[render-kanban-guard] blocked:"))
            # execute_code uses the `code` key
            result = self.mod.on_pre_tool_call(
                tool_name="execute_code", args={"code": "os.system('git push origin main')"})
            self.assertEqual(result["action"], "block")
            # benign + other tools -> None
            self.assertIsNone(self.mod.on_pre_tool_call(
                tool_name="terminal", args={"command": "git push -u origin wt/t_1"}))
            self.assertIsNone(self.mod.on_pre_tool_call(
                tool_name="write_file", args={"path": "x", "content": "git push origin main"}))
            self.assertIsNone(self.mod.on_pre_tool_call(tool_name="terminal", args=None))
            # merge rule depends on HERMES_PROFILE
            self.assertEqual(self.mod.on_pre_tool_call(
                tool_name="terminal", args={"command": "gh pr merge 3 --squash"})["action"], "block")
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_PROFILE": "reviewer"}):
            self.assertIsNone(self.mod.on_pre_tool_call(
                tool_name="terminal", args={"command": "gh pr merge 3 --squash"}))


# ---------------------------------------------------------------------------
# (b) Anchor sync against a real bare remote
# ---------------------------------------------------------------------------


class AnchorSyncTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.mod._LAST_SYNC_ATTEMPT.clear()
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.anchor = root / "anchor"
        git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=root)
        git("init", "-q", "-b", "main", str(self.seed), cwd=root)
        (self.seed / "README.md").write_text("one\n", encoding="utf-8")
        git("add", "README.md", cwd=self.seed)
        git("commit", "-q", "-m", "one", cwd=self.seed)
        git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        git("push", "-q", "-u", "origin", "main", cwd=self.seed)
        git("clone", "-q", str(self.remote), str(self.anchor), cwd=root)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.anchor), "main")

    def tearDown(self):
        self.tmp.cleanup()

    def advance_remote(self, name="two"):
        (self.seed / "README.md").write_text(f"{name}\n", encoding="utf-8")
        git("add", "README.md", cwd=self.seed)
        git("commit", "-q", "-m", name, cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)
        return git("rev-parse", "HEAD", cwd=self.seed)

    def test_fast_forwards_anchor_main_to_remote(self):
        before = git("rev-parse", "HEAD", cwd=self.anchor)
        remote_tip = self.advance_remote()
        self.assertNotEqual(before, remote_tip)
        with self.assertLogs(self.mod.logger, level="INFO") as logs:
            status = self.mod.sync_anchor(str(self.anchor))
        self.assertEqual(status, "synced")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.anchor), remote_tip)
        self.assertEqual(git("rev-parse", "main", cwd=self.anchor), remote_tip)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.anchor), "main")
        line = [l for l in logs.output if "anchor" in l and ".." in l][0]
        self.assertIn(f"{before[:7]}..{remote_tip[:7]}", line)

    def test_unchanged_when_already_current(self):
        self.assertEqual(self.mod.sync_anchor(str(self.anchor)), "unchanged")

    def test_dirty_anchor_is_skipped_and_untouched(self):
        (self.anchor / "README.md").write_text("local edit\n", encoding="utf-8")
        before = git("rev-parse", "HEAD", cwd=self.anchor)
        remote_tip = self.advance_remote()
        with self.assertLogs(self.mod.logger, level="WARNING") as logs:
            status = self.mod.sync_anchor(str(self.anchor))
        self.assertEqual(status, "skipped:dirty")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.anchor), before)
        self.assertNotEqual(git("rev-parse", "HEAD", cwd=self.anchor), remote_tip)
        self.assertEqual((self.anchor / "README.md").read_text(encoding="utf-8"), "local edit\n")
        self.assertTrue(any("local modifications" in l for l in logs.output))

    def test_untracked_files_do_not_count_as_dirty(self):
        (self.anchor / "scratch.txt").write_text("x\n", encoding="utf-8")
        remote_tip = self.advance_remote()
        self.assertEqual(self.mod.sync_anchor(str(self.anchor)), "synced")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.anchor), remote_tip)

    def test_rate_limited_second_call_within_window(self):
        clock = iter([100.0, 110.0, 200.0])
        now = lambda: next(clock)  # noqa: E731
        self.assertEqual(self.mod.sync_anchor(str(self.anchor), now=now), "unchanged")
        self.assertEqual(self.mod.sync_anchor(str(self.anchor), now=now), "skipped:rate-limited")
        remote_tip = self.advance_remote()
        self.assertEqual(self.mod.sync_anchor(str(self.anchor), now=now), "synced")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.anchor), remote_tip)

    def test_not_a_repo_is_skipped(self):
        with TemporaryDirectory() as plain:
            self.assertEqual(self.mod.sync_anchor(plain), "skipped:not-a-repo")
            self.assertEqual(self.mod.sync_anchor(os.path.join(plain, "missing")), "skipped:not-a-repo")

    def test_merge_in_progress_is_skipped(self):
        (self.anchor / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        with self.assertLogs(self.mod.logger, level="WARNING"):
            self.assertEqual(self.mod.sync_anchor(str(self.anchor)), "skipped:in-progress-op")

    def test_fetch_contract_via_recording_runner(self):
        calls = []

        def runner(argv, *, cwd, timeout, env=None):
            calls.append((list(argv), cwd, timeout, dict(env or {})))
            return self.mod._default_run(argv, cwd=cwd, timeout=timeout, env=env)

        self.advance_remote()
        self.assertEqual(self.mod.sync_anchor(str(self.anchor), run=runner), "synced")
        fetches = [c for c in calls if c[0][:2] == ["git", "fetch"]]
        self.assertEqual(len(fetches), 1)
        argv, cwd, timeout, env = fetches[0]
        self.assertEqual(argv, ["git", "fetch", "origin", "main"])
        self.assertNotIn("--prune", argv)
        self.assertNotIn("-p", argv)
        self.assertEqual(timeout, 45)
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        checkouts = [c[0] for c in calls if c[0][:2] == ["git", "checkout"]]
        self.assertEqual(checkouts, [["git", "checkout", "-q", "-B", "main", "origin/main"]])
        self.assertLess(calls.index(fetches[0]), [c[0] for c in calls].index(checkouts[0]))

    def test_fetch_failure_never_raises(self):
        git("remote", "set-url", "origin", str(Path(self.tmp.name) / "nope.git"), cwd=self.anchor)
        with self.assertLogs(self.mod.logger, level="WARNING"):
            self.assertEqual(self.mod.sync_anchor(str(self.anchor)), "failed:fetch")

    def test_timeout_is_reported_not_raised(self):
        def runner(argv, *, cwd, timeout, env=None):
            if argv[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(argv, timeout)
            return self.mod._default_run(argv, cwd=cwd, timeout=timeout, env=env)

        with self.assertLogs(self.mod.logger, level="WARNING"):
            self.assertEqual(self.mod.sync_anchor(str(self.anchor), run=runner), "failed:timeout")

    def test_claimed_hook_syncs_only_in_dispatcher(self):
        ctx = StubCtx({"anchor_repos": [str(self.anchor)]})
        self.mod.register(ctx)
        remote_tip = self.advance_remote()
        # Worker process (HERMES_KANBAN_TASK set): must not touch the anchor.
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1"}):
            self.mod.on_kanban_task_claimed(task_id="t_1", profile_name="custom",
                                            board="default", assignee="coder", run_id=1)
        self.assertNotEqual(git("rev-parse", "HEAD", cwd=self.anchor), remote_tip)
        # Dispatcher process: syncs.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_KANBAN_TASK", None)
            self.mod.on_kanban_task_claimed(task_id="t_1", profile_name="custom",
                                            board="default", assignee="coder", run_id=1,
                                            telemetry_schema_version=1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.anchor), remote_tip)

    def test_claim_time_sync_produces_fresh_worktree_base(self):
        """Regression: the claim-time sync must bring the anchor to origin/main
        BEFORE the dispatcher cuts a worktree from HEAD. A stale anchor would
        produce a worktree behind main. This test proves the sync moves HEAD
        to the remote tip, which is what _ensure_git_worktree would cut from."""
        ctx = StubCtx({"anchor_repos": [str(self.anchor)]})
        self.mod.register(ctx)
        # Anchor is behind; remote has advanced.
        stale = git("rev-parse", "HEAD", cwd=self.anchor)
        fresh = self.advance_remote()
        self.assertNotEqual(stale, fresh)
        # Simulate the dispatcher's kanban_task_claimed hook firing.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_KANBAN_TASK", None)
            self.mod.on_kanban_task_claimed(
                task_id="t_x", profile_name="custom",
                board="default", assignee="coder", run_id=1,
            )
        # The anchor's HEAD is now at the remote tip — a worktree cut from
        # this HEAD would start on fresh main, not stale.
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.anchor), fresh)
        self.assertEqual(git("rev-parse", "main", cwd=self.anchor), fresh)


# ---------------------------------------------------------------------------
# (c) System prompt section
# ---------------------------------------------------------------------------


class PromptSectionTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.ctx = StubCtx()
        self.mod.register(self.ctx)
        self.provider = self.ctx.sections["render-kanban-sdlc-role"][0]

    def info(self, profile):
        return {"session_id": "s", "model": "m", "provider": "p", "platform": "cli",
                "profile_name": profile, "cwd": "/x"}

    def test_empty_without_kanban_task(self):
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "coder"}, clear=False):
            os.environ.pop("HERMES_KANBAN_TASK", None)
            self.assertEqual(self.provider(self.info("coder")), "")

    def test_worker_text_for_coder(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_PROFILE": "coder"}):
            text = self.provider(self.info("coder"))
        self.assertIn('skill_view("kanban-sdlc-worker")', text)
        self.assertIn("kanban_request_review", text)
        self.assertIn("kanban_block", text)
        self.assertIn("protocol violation", text)
        self.assertIn("never push to main", text)
        self.assertNotIn("kanban-sdlc-reviewer", text)

    def test_worker_text_from_session_info_when_env_profile_missing(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1"}, clear=False):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(self.provider(self.info("coder")), self.mod.WORKER_SECTION)

    def test_reviewer_text(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_PROFILE": "reviewer"}):
            text = self.provider(self.info("reviewer"))
        self.assertIn("skill_view(\"kanban-sdlc-reviewer\")", text)
        self.assertIn("sdlc-review", text)
        for tool in ("kanban_complete", "kanban_request_changes", "kanban_block"):
            self.assertIn(tool, text)

    def test_other_profiles_get_nothing(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1", "HERMES_PROFILE": "engine-research"}):
            self.assertEqual(self.provider(self.info("engine-research")), "")
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1"}, clear=False):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(self.provider(self.info("custom")), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
