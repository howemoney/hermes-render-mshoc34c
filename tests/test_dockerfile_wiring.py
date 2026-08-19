"""Wiring tests for the kanban SDLC pieces baked by the Dockerfile.

Two layers:

1. Static: the Dockerfile carries the four COPY lines (plugin dir, pre-push
   hook, health probe, 017 boot hook), the boot hook is named so it sorts
   after 016 and before upstream 02-reconcile-profiles, both shell scripts
   are `sh -n` clean, and render.yaml carries GH_TOKEN / pro / 10 GB.

2. Behavioural: 017-render-kanban-bootstrap is dry-run against a temp tree
   (fake `hermes`, fake `gh`, real `git`) to prove the properties the header
   promises -- profiles + SOUL.md seeded once, board workdir set once, anchor
   hooks/identity set, main fast-forwarded only when clean, health probe
   copied once, exit 0 even when things are missing. The script exposes
   RENDER_TOOLS_DIR / HERMES_BIN / HERMES_VENV_PYTHON overrides for exactly
   this purpose.

Run:  python3 -m unittest tests.test_dockerfile_wiring -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
RENDER_YAML = REPO_ROOT / "render.yaml"
BOOTSTRAP = REPO_ROOT / "scripts" / "cont-init-kanban-bootstrap.sh"
PRE_PUSH = REPO_ROOT / "scripts" / "git-hooks" / "pre-push"

EXPECTED_COPY_LINES = (
    "COPY --chown=root:root plugins/render-kanban-guard/ /opt/hermes/plugins/render-kanban-guard/",
    "COPY --chmod=0755 scripts/git-hooks/pre-push /opt/render-tools/git-hooks/pre-push",
    "COPY --chmod=0755 scripts/kanban-health.py /opt/render-tools/scripts/kanban-health.py",
    "COPY --chmod=0755 scripts/cont-init-kanban-bootstrap.sh /etc/cont-init.d/017-render-kanban-bootstrap",
)

FAKE_SOUL_DEFAULT = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct.\n"
)


def _sh_candidates():
    # dash is what /bin/sh is on the Debian base; check it when present.
    return [s for s in ("sh", "dash") if shutil.which(s)]


def _shell_for_run() -> str:
    return shutil.which("dash") or shutil.which("sh") or "sh"


class DockerfileWiringTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    def test_four_copy_lines_present(self):
        for line in EXPECTED_COPY_LINES:
            self.assertIn(line, self.dockerfile, f"Dockerfile is missing:\n  {line}")

    def test_boot_hook_numbering(self):
        # 016 patches root config first; 017 must sort after it and before
        # upstream's 02-reconcile-profiles (lexicographic: "017" < "02").
        self.assertIn("/etc/cont-init.d/016-render-patch-config", self.dockerfile)
        self.assertIn("/etc/cont-init.d/017-render-kanban-bootstrap", self.dockerfile)
        self.assertLess("016-render-patch-config", "017-render-kanban-bootstrap")
        self.assertLess("017-render-kanban-bootstrap", "02-reconcile-profiles")
        pos16 = self.dockerfile.index("016-render-patch-config")
        pos17 = self.dockerfile.index("017-render-kanban-bootstrap")
        self.assertLess(pos16, pos17, "017 COPY should follow the 016 block")

    def test_build_time_sanity_run_present(self):
        # The RUN that fails the BUILD on a broken script (never the boot).
        self.assertIn("sh -n /etc/cont-init.d/017-render-kanban-bootstrap", self.dockerfile)
        self.assertIn("sh -n /opt/render-tools/git-hooks/pre-push", self.dockerfile)
        self.assertIn("/opt/render-tools/scripts/kanban-health.py').read_text()", self.dockerfile)
        self.assertIn("install -d -o root -g root -m 0755 /opt/render-tools/git-hooks /opt/render-tools/scripts", self.dockerfile)

    def test_copy_sources_exist_in_repo(self):
        # A COPY whose source is missing fails `docker build`; catch it here
        # (these sources are owned by sibling components of PR-B).
        for m in re.finditer(r"^COPY\s+(?:--[^\s]+\s+)*(\S+)\s+\S+\s*$", self.dockerfile, re.M):
            src = m.group(1)
            if src.startswith("--from"):
                continue
            self.assertTrue((REPO_ROOT / src).exists(), f"COPY source missing in repo: {src}")

    def test_hermes_symlink_comment_mentions_dispatcher_fallback(self):
        self.assertIn("sys.executable -m hermes_cli.main", self.dockerfile)
        self.assertIn("belt-and-braces", self.dockerfile)

    def test_bootstrap_script_is_sh_clean(self):
        src = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertTrue(src.startswith("#!/command/with-contenv sh\n"))
        self.assertIn("set -eu", src)
        self.assertTrue(os.access(BOOTSTRAP, os.X_OK), "bootstrap script must be executable")
        for shell in _sh_candidates():
            r = subprocess.run([shell, "-n", str(BOOTSTRAP)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(r.returncode, 0, f"{shell} -n failed on bootstrap: {r.stderr}")

    def test_bootstrap_never_prunes_and_exits_zero(self):
        src = BOOTSTRAP.read_text(encoding="utf-8")
        # Only inspect executable lines: comments/strings may *talk* about
        # pushing and pruning; the commands must not do it.
        code_lines = [ln for ln in src.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        self.assertNotRegex(code, r"fetch[^\n]*--prune", "bootstrap must never fetch --prune (worktree cleanup hazard)")
        self.assertNotRegex(code, r"\bgit\b[^\n\"']*\bpush\b", "bootstrap must never push")
        self.assertNotRegex(code, r"--force\b|--hard\b", "bootstrap must never force anything")
        self.assertTrue(src.rstrip().endswith("exit 0"))

    def test_pre_push_hook_content_valid(self):
        src = PRE_PUSH.read_text(encoding="utf-8")
        self.assertTrue(src.startswith("#!/bin/sh\n"))
        mode = PRE_PUSH.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "pre-push must be executable")
        self.assertIn("refs/heads/main|refs/heads/master", src)
        for shell in _sh_candidates():
            r = subprocess.run([shell, "-n", str(PRE_PUSH)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(r.returncode, 0, f"{shell} -n failed on pre-push: {r.stderr}")


class RenderYamlTests(unittest.TestCase):
    def setUp(self):
        self.text = RENDER_YAML.read_text(encoding="utf-8")

    def test_gh_token_declared_unsynced(self):
        self.assertRegex(self.text, r"- key: GH_TOKEN\n\s+sync: false")

    def test_plan_and_disk_match_live_service(self):
        self.assertRegex(self.text, re.compile(r"^\s+plan: pro\s*$", re.M))
        self.assertRegex(self.text, re.compile(r"^\s+sizeGB: 10\s*$", re.M))
        self.assertNotRegex(self.text, re.compile(r"^\s+plan: standard\s*$", re.M))

    def test_dispatch_env_override_absent(self):
        # Only a comment may mention it; never a `- key:` entry.
        self.assertNotRegex(self.text, r"- key: HERMES_KANBAN_DISPATCH_IN_GATEWAY")

    def test_oidc_scopes_still_present(self):
        self.assertIn("HERMES_DASHBOARD_OIDC_SCOPES", self.text)


FAKE_HERMES = r"""#!/bin/sh
# Fake `hermes` for the bootstrap dry-run: records argv, mimics the on-disk
# side effects of the three subcommands the boot hook uses.
printf '%s\n' "$*" >> "${HERMES_FAKE_LOG}"
case "${1:-} ${2:-}" in
  "profile create")
    name="$3"; shift 3; desc=""
    while [ $# -gt 0 ]; do
      case "$1" in --description) desc="$2"; shift 2 ;; *) shift ;; esac
    done
    d="${HERMES_HOME}/profiles/${name}"
    mkdir -p "$d/skills"
    printf '%s' "${FAKE_SOUL_DEFAULT}" > "$d/SOUL.md"
    printf '# per-profile secrets\n' > "$d/.env"
    [ -n "$desc" ] && printf 'description: %s\ndescription_auto: false\n' "$desc" > "$d/profile.yaml"
    ;;
  "profile describe")
    name="$3"; shift 3; text=""
    while [ $# -gt 0 ]; do
      case "$1" in --text) text="$2"; shift 2 ;; *) shift ;; esac
    done
    if [ "$name" = default ]; then d="${HERMES_HOME}"; else d="${HERMES_HOME}/profiles/${name}"; fi
    printf 'description: %s\ndescription_auto: false\n' "$text" > "$d/profile.yaml"
    ;;
  "kanban boards")
    # $3 = set-default-workdir, $4 = slug, $5 = path
    mkdir -p "${HERMES_HOME}/kanban/boards/$4"
    printf '{"slug": "%s", "default_workdir": "%s"}\n' "$4" "$5" > "${HERMES_HOME}/kanban/boards/$4/board.json"
    ;;
esac
exit 0
"""

FAKE_PATCH_CONFIG = r"""#!/bin/sh
printf '%s\n' "$*" >> "${PATCH_CONFIG_FAKE_LOG}"
exit 0
"""


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class BootstrapDryRunTests(unittest.TestCase):
    """Drive scripts/cont-init-kanban-bootstrap.sh against a temp tree."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git not available")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.tools = self.root / "tools"
        self.bin = self.root / "bin"
        for d in (self.data, self.tools / "git-hooks", self.tools / "scripts", self.bin):
            d.mkdir(parents=True)

        # Tool tree as the Dockerfile lays it out.
        shutil.copy2(PRE_PUSH, self.tools / "git-hooks" / "pre-push")
        (self.tools / "git-hooks" / "pre-push").chmod(0o755)
        _write_exec(self.tools / "scripts" / "kanban-health.py", "#!/usr/bin/env python3\nprint('probe')\n")
        _write_exec(self.tools / "patch-config.py", FAKE_PATCH_CONFIG)

        # Fakes on PATH.
        self.hermes_log = self.root / "hermes.log"
        self.patch_log = self.root / "patch.log"
        _write_exec(self.bin / "hermes", FAKE_HERMES)
        _write_exec(self.bin / "gh", "#!/bin/sh\nexit 0\n")
        if shutil.which("timeout") is None:
            # macOS has no coreutils `timeout`; the box does (debian:13.4).
            _write_exec(self.bin / "timeout", "#!/bin/sh\nshift\nexec \"$@\"\n")

        self.env = dict(os.environ)
        self.env.update(
            {
                "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                "HERMES_HOME": str(self.data),
                "RENDER_TOOLS_DIR": str(self.tools),
                "HERMES_BIN": str(self.bin / "hermes"),
                "HERMES_VENV_PYTHON": sys.executable,
                "HERMES_FAKE_LOG": str(self.hermes_log),
                "PATCH_CONFIG_FAKE_LOG": str(self.patch_log),
                "FAKE_SOUL_DEFAULT": FAKE_SOUL_DEFAULT,
                # Hermetic git.
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(self.root / "no-global-gitconfig"),
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
        )
        self.env.pop("HOME", None)  # the script sets HOME=$HERMES_HOME itself

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(cwd), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def ok(self, r: subprocess.CompletedProcess) -> str:
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip()

    def make_anchor(self) -> tuple[Path, Path, Path]:
        """bare remote + anchor clone (behind) + a second clone that advances main."""
        remote = self.root / "remote.git"
        self.ok(self.git(self.root, "init", "--bare", "-q", "-b", "main", str(remote)))
        seed = self.root / "seed"
        self.ok(self.git(self.root, "clone", "-q", str(remote), str(seed)))
        (seed / "README").write_text("v1\n", encoding="utf-8")
        self.ok(self.git(seed, "add", "README"))
        self.ok(self.git(seed, "commit", "-q", "-m", "v1"))
        self.ok(self.git(seed, "branch", "-M", "main"))
        self.ok(self.git(seed, "push", "-q", "-u", "origin", "main"))

        anchor = self.data / "work" / "stopsargassum"
        anchor.parent.mkdir(parents=True, exist_ok=True)
        self.ok(self.git(self.root, "clone", "-q", str(remote), str(anchor)))
        return remote, anchor, seed

    def advance_remote(self, seed: Path, msg: str) -> str:
        (seed / "README").write_text(msg + "\n", encoding="utf-8")
        self.ok(self.git(seed, "commit", "-q", "-am", msg))
        self.ok(self.git(seed, "push", "-q", "origin", "main"))
        return self.ok(self.git(seed, "rev-parse", "HEAD"))

    def run_bootstrap(self) -> subprocess.CompletedProcess:
        r = subprocess.run([_shell_for_run(), str(BOOTSTRAP)], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        return r

    # -- tests -----------------------------------------------------------
    def test_first_boot_provisions_everything(self):
        remote, anchor, seed = self.make_anchor()
        tip = self.advance_remote(seed, "v2")  # anchor is now behind origin/main
        before = self.ok(self.git(anchor, "rev-parse", "HEAD"))
        self.assertNotEqual(before, tip)

        r = self.run_bootstrap()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn("[render-tools] kanban-bootstrap: done", out)

        # (a) profiles created once, with --no-alias + --description, SOUL seeded.
        log = self.hermes_log.read_text(encoding="utf-8")
        self.assertIn("profile create coder --no-alias --description ", log)
        self.assertIn("profile create reviewer --no-alias --description ", log)
        coder_soul = (self.data / "profiles" / "coder" / "SOUL.md").read_text(encoding="utf-8")
        reviewer_soul = (self.data / "profiles" / "reviewer" / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("skill_view('kanban-sdlc-worker')", coder_soul)
        self.assertIn("kanban_request_review", coder_soul)
        self.assertIn("never merge", coder_soul)
        self.assertIn("skill_view('kanban-sdlc-reviewer')", reviewer_soul)
        self.assertIn("kanban_complete", reviewer_soul)
        self.assertIn("kanban_request_changes", reviewer_soul)
        self.assertEqual(len(coder_soul.strip().splitlines()), 5)
        self.assertEqual(len(reviewer_soul.strip().splitlines()), 5)
        self.assertNotIn("Nous Research", coder_soul)

        # patch-config --profile-config for both.
        patch = self.patch_log.read_text(encoding="utf-8")
        self.assertIn(f"--profile-config {self.data}/profiles/coder/config.yaml --profile coder", patch)
        self.assertIn(f"--profile-config {self.data}/profiles/reviewer/config.yaml --profile reviewer", patch)

        # (b) default description set (was missing); coder/reviewer already
        # carried one from create, so no extra describe for them.
        self.assertIn("profile describe default --text Orchestrator and reviewer-of-last-resort", log)
        self.assertNotIn("profile describe coder", log)
        self.assertNotIn("profile describe reviewer", log)

        # (c) board workdir.
        self.assertIn(f"kanban boards set-default-workdir default {anchor}", log)
        board = json.loads((self.data / "kanban" / "boards" / "default" / "board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["default_workdir"], str(anchor))

        # (d) anchor config + fast-forward.
        self.assertEqual(self.ok(self.git(anchor, "config", "--local", "core.hooksPath")), str(self.tools / "git-hooks"))
        self.assertEqual(self.ok(self.git(anchor, "config", "user.name")), "Howe Agency Bot")
        self.assertEqual(self.ok(self.git(anchor, "config", "user.email")), "snhowe@gmail.com")
        # remote is a local path, not https -> helper must NOT be set.
        r2 = self.git(anchor, "config", "--get-all", "credential.helper")
        self.assertEqual(r2.stdout.strip(), "")
        self.assertIn("not https", out)
        self.assertEqual(self.ok(self.git(anchor, "rev-parse", "HEAD")), tip)
        self.assertEqual(self.ok(self.git(anchor, "rev-parse", "--abbrev-ref", "HEAD")), "main")
        self.assertRegex(out, r"anchor main: [0-9a-f]+ -> [0-9a-f]+ \(origin/main\)")
        self.assertIn("worktree prune ok", out)

        # (e) health probe + npm cache.
        self.assertTrue((self.data / "scripts" / "kanban-health.py").is_file())
        self.assertTrue(os.access(self.data / "scripts" / "kanban-health.py", os.X_OK))
        self.assertTrue((self.data / ".npm-cache").is_dir())

    def test_second_boot_is_idempotent_and_respects_operator_edits(self):
        remote, anchor, seed = self.make_anchor()
        self.ok(self.run_bootstrap())  # first boot
        self.hermes_log.write_text("", encoding="utf-8")
        self.patch_log.write_text("", encoding="utf-8")

        # Operator customisations that a second boot must leave alone.
        soul = self.data / "profiles" / "coder" / "SOUL.md"
        soul.write_text("operator-authored persona\n", encoding="utf-8")
        probe = self.data / "scripts" / "kanban-health.py"
        probe.write_text("# tuned on the box\n", encoding="utf-8")
        self.ok(self.git(anchor, "config", "--local", "user.email", "someone@else.example"))

        r = self.run_bootstrap()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        log = self.hermes_log.read_text(encoding="utf-8")

        self.assertNotIn("profile create", log)
        self.assertNotIn("creating profile", out)
        self.assertNotIn("set-default-workdir", log)
        self.assertIn("default_workdir already set", out)
        self.assertEqual(soul.read_text(encoding="utf-8"), "operator-authored persona\n")
        self.assertEqual(probe.read_text(encoding="utf-8"), "# tuned on the box\n")
        self.assertEqual(self.ok(self.git(anchor, "config", "user.email")), "someone@else.example")
        self.assertEqual(self.ok(self.git(anchor, "config", "user.name")), "Howe Agency Bot")
        # patch-config still runs every boot (it owns insert-vs-enforce).
        self.assertIn("--profile coder", self.patch_log.read_text(encoding="utf-8"))

    def test_upstream_default_soul_is_replaced_but_custom_is_kept(self):
        # Simulates a profile that exists (e.g. created by hand / a timed-out
        # create) but still carries upstream's DEFAULT_SOUL_MD.
        self.make_anchor()
        for name in ("coder", "reviewer"):
            d = self.data / "profiles" / name
            d.mkdir(parents=True)
            (d / "SOUL.md").write_text(FAKE_SOUL_DEFAULT, encoding="utf-8")
        (self.data / "profiles" / "reviewer" / "SOUL.md").write_text("custom\n", encoding="utf-8")
        self.ok(self.run_bootstrap())
        self.assertIn("kanban-sdlc-worker", (self.data / "profiles" / "coder" / "SOUL.md").read_text(encoding="utf-8"))
        self.assertEqual((self.data / "profiles" / "reviewer" / "SOUL.md").read_text(encoding="utf-8"), "custom\n")
        # No create was attempted because the dirs existed.
        self.assertNotIn("profile create", self.hermes_log.read_text(encoding="utf-8"))

    def test_dirty_anchor_is_not_synced(self):
        remote, anchor, seed = self.make_anchor()
        self.ok(self.run_bootstrap())  # clean first boot
        tip = self.advance_remote(seed, "v3")
        before = self.ok(self.git(anchor, "rev-parse", "HEAD"))
        (anchor / "README").write_text("local uncommitted edit\n", encoding="utf-8")

        r = self.run_bootstrap()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dirty or mid-merge/rebase; NOT syncing", r.stderr)
        self.assertEqual(self.ok(self.git(anchor, "rev-parse", "HEAD")), before)
        self.assertNotEqual(before, tip)
        self.assertEqual((anchor / "README").read_text(encoding="utf-8"), "local uncommitted edit\n")

    def test_merge_in_progress_is_not_synced(self):
        remote, anchor, seed = self.make_anchor()
        self.ok(self.run_bootstrap())
        before = self.ok(self.git(anchor, "rev-parse", "HEAD"))
        git_dir = Path(self.ok(self.git(anchor, "rev-parse", "--absolute-git-dir")))
        (git_dir / "MERGE_HEAD").write_text(before + "\n", encoding="utf-8")
        self.advance_remote(seed, "v4")
        r = self.run_bootstrap()
        self.assertEqual(r.returncode, 0)
        self.assertIn("NOT syncing", r.stderr)
        self.assertEqual(self.ok(self.git(anchor, "rev-parse", "HEAD")), before)

    def test_untracked_files_do_not_block_sync(self):
        remote, anchor, seed = self.make_anchor()
        tip = self.advance_remote(seed, "v5")
        (anchor / "stray.log").write_text("x\n", encoding="utf-8")
        self.ok(self.run_bootstrap())
        self.assertEqual(self.ok(self.git(anchor, "rev-parse", "HEAD")), tip)

    def test_https_remote_gets_gh_credential_helper_and_fetch_failure_is_soft(self):
        remote, anchor, seed = self.make_anchor()
        before = self.ok(self.git(anchor, "rev-parse", "HEAD"))
        # An https remote nobody answers: connection refused -> fetch fails
        # fast; the script must warn, keep HEAD, and still exit 0.
        self.ok(self.git(anchor, "remote", "set-url", "origin", "https://127.0.0.1:9/howemoney/stopsargassum.git"))
        r = self.run_bootstrap()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.ok(self.git(anchor, "config", "--get", "credential.helper")), "!gh auth git-credential")
        self.assertIn("anchor fetch failed or timed out", r.stderr)
        self.assertEqual(self.ok(self.git(anchor, "rev-parse", "HEAD")), before)
        self.assertIn("kanban-bootstrap: done", r.stdout)

    def test_existing_credential_helper_is_left_alone(self):
        remote, anchor, seed = self.make_anchor()
        self.ok(self.git(anchor, "remote", "set-url", "origin", "https://127.0.0.1:9/x.git"))
        self.ok(self.git(anchor, "config", "--local", "credential.helper", "store"))
        self.ok(self.run_bootstrap())
        self.assertEqual(self.git(anchor, "config", "--get-all", "credential.helper").stdout.strip(), "store")

    def test_no_anchor_no_hermes_still_exits_zero(self):
        # Fresh disk: no anchor clone, no hermes CLI. Everything degrades to
        # log lines; profiles/board steps are skipped; exit 0.
        self.env["HERMES_BIN"] = str(self.root / "nope")
        self.env["PATH"] = str(self.root / "empty-bin") + os.pathsep + "/usr/bin:/bin"
        (self.root / "empty-bin").mkdir()
        if shutil.which("timeout", path=self.env["PATH"]) is None:
            _write_exec(self.root / "empty-bin" / "timeout", "#!/bin/sh\nshift\nexec \"$@\"\n")
        r = self.run_bootstrap()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn("hermes CLI not found", out)
        self.assertIn("not present", out)
        self.assertIn("kanban-bootstrap: done", out)
        self.assertFalse((self.data / "profiles").exists())
        # (e) still happens: it needs neither hermes nor the anchor.
        self.assertTrue((self.data / ".npm-cache").is_dir())
        self.assertTrue((self.data / "scripts" / "kanban-health.py").is_file())


if __name__ == "__main__":
    unittest.main()
