"""Behavioural tests for scripts/git-hooks/pre-push.

The hook is wired into the kanban anchor repo via ``core.hooksPath`` by the
017-render-kanban-bootstrap cont-init hook, so every worker worktree inherits
it. These tests drive a real ``git push`` against a temp bare remote -- not a
static grep -- because the failure mode we care about ("push to main went
through") only shows up when git actually invokes the hook.

Run:  python3 -m unittest tests.test_git_hooks -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "scripts" / "git-hooks"
PRE_PUSH = HOOKS_DIR / "pre-push"


def _git_env(home: Path) -> dict:
    """Hermetic git environment: no user/global config, no system hooks."""
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig-none"),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(cwd: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class PrePushHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git not available")

    def setUp(self):
        self.assertTrue(PRE_PUSH.is_file(), f"missing {PRE_PUSH}")
        # The Dockerfile COPYs with --chmod=0755, but the clone on the box
        # must keep the bit too or `git push` silently ignores the hook
        # (git warns "hook was ignored because it's not set as executable").
        self.assertTrue(
            os.access(PRE_PUSH, os.X_OK),
            f"{PRE_PUSH} is not executable; run `chmod 0755 {PRE_PUSH}`",
        )

        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.env = _git_env(self.home)

        self.remote = root / "remote.git"
        r = _git(root, "init", "--bare", "-q", "-b", "main", str(self.remote), env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

        self.clone = root / "clone"
        r = _git(root, "clone", "-q", str(self.remote), str(self.clone), env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

        # Seed one commit on main so there is something to push.
        (self.clone / "README").write_text("seed\n", encoding="utf-8")
        for cmd in (("add", "README"), ("commit", "-q", "-m", "seed")):
            r = _git(self.clone, *cmd, env=self.env)
            self.assertEqual(r.returncode, 0, r.stderr)
        # Make sure the local branch is literally `main` regardless of the
        # host's init.defaultBranch.
        r = _git(self.clone, "branch", "-M", "main", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

        # Exactly what 017-render-kanban-bootstrap does on the anchor repo.
        r = _git(self.clone, "config", "--local", "core.hooksPath", str(HOOKS_DIR), env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        self._tmp.cleanup()

    def test_push_to_main_is_refused_with_clear_message(self):
        r = _git(self.clone, "push", "origin", "HEAD:main", env=self.env)
        self.assertNotEqual(r.returncode, 0, "push to main must fail:\n" + r.stdout + r.stderr)
        self.assertIn("REFUSED update of refs/heads/main", r.stderr)
        self.assertIn("wt/<task-id>", r.stderr)
        # And nothing landed on the remote.
        r = _git(self.remote, "rev-parse", "--verify", "-q", "refs/heads/main", env=self.env)
        self.assertNotEqual(r.returncode, 0, "remote main should not exist after a refused push")

    def test_push_to_master_is_refused_too(self):
        r = _git(self.clone, "push", "origin", "HEAD:master", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED update of refs/heads/master", r.stderr)

    def test_push_to_wt_branch_succeeds(self):
        r = _git(self.clone, "push", "origin", "HEAD:wt/x", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        r = _git(self.remote, "rev-parse", "--verify", "-q", "refs/heads/wt/x", env=self.env)
        self.assertEqual(r.returncode, 0, "remote wt/x should exist after an allowed push")

    def test_mixed_push_is_refused_atomically(self):
        # One refspec allowed + one forbidden in the same push: the hook sees
        # both lines on stdin and must reject the whole push (git aborts all
        # refs when pre-push exits non-zero).
        r = _git(self.clone, "push", "origin", "HEAD:wt/y", "HEAD:main", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED update of refs/heads/main", r.stderr)
        r = _git(self.remote, "rev-parse", "--verify", "-q", "refs/heads/wt/y", env=self.env)
        self.assertNotEqual(r.returncode, 0, "refused push must not land any ref")

    def test_hook_is_inherited_by_linked_worktrees(self):
        # core.hooksPath lives in the shared .git/config, so a worktree cut
        # the way the dispatcher does it (`git worktree add <dir> -b wt/<id>`)
        # is guarded without any per-worktree setup.
        wt = Path(self._tmp.name) / "wt-t_abc"
        r = _git(self.clone, "worktree", "add", "-q", "-b", "wt/t_abc", str(wt), env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _git(wt, "push", "origin", "HEAD:main", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED update of refs/heads/main", r.stderr)
        r = _git(wt, "push", "-u", "origin", "wt/t_abc", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_verify_bypasses_hook_by_design(self):
        # Documents the stated limitation: the hook is an accident guard, not
        # a security boundary. The render-kanban-guard plugin blocks
        # `--no-verify` at the tool layer for kanban workers.
        r = _git(self.clone, "push", "--no-verify", "origin", "HEAD:main", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class PrePushHookSourceTests(unittest.TestCase):
    def test_posix_sh_clean(self):
        src = PRE_PUSH.read_text(encoding="utf-8")
        self.assertTrue(src.startswith("#!/bin/sh\n"), "pre-push must be POSIX sh (dash on the box)")
        for shell in ("sh", "dash"):
            if shutil.which(shell) is None:
                continue
            r = subprocess.run([shell, "-n", str(PRE_PUSH)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(r.returncode, 0, f"{shell} -n failed: {r.stderr}")

    def test_documents_scope(self):
        src = PRE_PUSH.read_text(encoding="utf-8")
        self.assertIn("core.hooksPath", src)
        self.assertIn("not a security boundary", src)
        self.assertIn("refs/heads/main|refs/heads/master", src)


if __name__ == "__main__":
    unittest.main()
