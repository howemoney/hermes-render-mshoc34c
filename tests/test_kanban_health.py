"""Tests for scripts/kanban-health.py (Workstream E, the zero-LLM board probe).

The script is split on purpose: ``evaluate(inputs, state)`` is a pure
function of dict fixtures, ``read_db`` reads a real sqlite board, and
``run()`` glues them for the cron. These tests cover all three layers
without Hermes installed:

  * rule table against hand-written ``inputs`` dicts (fingerprints are
    new -> sent, unchanged -> silent, escalated -> sent again, cleared ->
    dropped so a recurrence re-alerts);
  * ``read_db`` against a throwaway sqlite DB with the same three tables
    the script queries (``tasks``, ``task_events``, ``task_runs``);
  * ``run(["--dry-run", ...])`` end-to-end with a missing ``hermes`` binary
    (diagnostics/stats unavailable -> those rules carry, nothing crashes,
    exit 0, state file untouched in dry-run, written otherwise).

The script file name has a hyphen, so it is loaded via importlib.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "kanban-health.py"


def _load():
    spec = importlib.util.spec_from_file_location("kanban_health", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kanban_health"] = mod
    spec.loader.exec_module(mod)
    return mod


kh = _load()
NOW = 1_800_000_000


def _inputs(**over) -> dict:
    base = {
        "now": NOW,
        "db_path": "/nonexistent/kanban.db",
        "worker_profiles": ["coder"],
        "diagnostics": [],
        "stats": {"by_status": {}, "by_assignee": {}, "oldest_ready_age_seconds": None, "now": NOW},
        "db": {
            "db_path": "/nonexistent/kanban.db",
            "running": [], "review": [], "blocked": [],
            "max_event_id": 0, "completed_events": [], "protocol_violations": [],
        },
        "lock": "held",
        "estop_age": None,
        "max_in_progress": 2,
        "github": {},
    }
    base.update(over)
    return base


def _steady_state() -> dict:
    """A state that has already seen one run, so cursor-based rules are live."""
    st = kh.empty_state()
    st["done_cursor"] = 0
    st["pv_cursor"] = 0
    st["last_run"] = NOW - 3 * 3600
    return st


class EvaluatePureTests(unittest.TestCase):
    def test_quiet_board_is_silent(self):
        msg, new_state, actions = kh.evaluate(_inputs(), _steady_state())
        self.assertEqual(msg, "")
        self.assertEqual(actions, [])
        self.assertEqual(new_state["fingerprints"], {})
        self.assertEqual(new_state["last_run"], NOW)

    def test_first_run_only_records_cursors_no_history_dump(self):
        db = _inputs()["db"]
        db["max_event_id"] = 51
        msg, st, _ = kh.evaluate(_inputs(db=db), kh.empty_state())
        self.assertEqual(msg, "")
        self.assertEqual(st["done_cursor"], 51)
        self.assertEqual(st["pv_cursor"], 51)

    def test_rule1_error_diag_sent_once_then_escalation_resends(self):
        diags = [{"task_id": "t_1", "title": "Card", "status": "running", "assignee": "coder",
                  "diagnostics": [{"kind": "stale_heartbeat", "severity": "error", "title": "no beat"}]}]
        st = _steady_state()
        msg1, st1, _ = kh.evaluate(_inputs(diagnostics=diags), st)
        self.assertIn("! diag error t_1 stale_heartbeat", msg1)
        msg2, st2, _ = kh.evaluate(_inputs(diagnostics=diags), st1)
        self.assertEqual(msg2, "", "same fingerprint must not re-alert")
        diags[0]["diagnostics"][0]["severity"] = "critical"
        msg3, st3, _ = kh.evaluate(_inputs(diagnostics=diags), st2)
        self.assertIn("escalated from error", msg3)
        # cleared -> fingerprint dropped -> recurrence alerts again
        _, st4, _ = kh.evaluate(_inputs(diagnostics=[]), st3)
        self.assertEqual(st4["fingerprints"], {})
        msg5, _, _ = kh.evaluate(_inputs(diagnostics=diags), st4)
        self.assertIn("t_1", msg5)

    def test_rule1_warning_diag_is_ignored_but_rule2_stranded_is_not(self):
        diags = [{"task_id": "t_2", "title": "x", "status": "ready", "assignee": "coder",
                  "diagnostics": [{"kind": "something", "severity": "warning", "title": "meh"}]}]
        msg, _, _ = kh.evaluate(_inputs(diagnostics=diags), _steady_state())
        self.assertEqual(msg, "")
        diags[0]["diagnostics"][0]["kind"] = "stranded_in_ready"
        msg, _, _ = kh.evaluate(_inputs(diagnostics=diags), _steady_state())
        self.assertIn("stranded_in_ready", msg)

    def test_rule3a_ready_stale_needs_two_rising_samples_and_respects_cap(self):
        stats = {"by_status": {"ready": 3, "running": 0}, "oldest_ready_age_seconds": 4000}
        st = _steady_state()
        msg1, st1, _ = kh.evaluate(_inputs(stats=stats), st)
        self.assertEqual(msg1, "", "first sample only records the age")
        self.assertEqual(st1["last_oldest_ready_age"], 4000)
        later = _inputs(stats={"by_status": {"ready": 3, "running": 0},
                               "oldest_ready_age_seconds": 4000 + 3 * 3600}, now=NOW + 3 * 3600)
        msg2, _, _ = kh.evaluate(later, st1)
        self.assertIn("is the in-gateway dispatcher ticking", msg2)
        # capped by max_in_progress -> not the dispatcher's fault -> silent
        capped = _inputs(stats={"by_status": {"ready": 3, "running": 2},
                                "oldest_ready_age_seconds": 4000 + 3 * 3600}, now=NOW + 3 * 3600)
        msg3, _, _ = kh.evaluate(capped, st1)
        self.assertEqual(msg3, "")

    def test_rule3b_lock_unheld_and_3c_estop(self):
        msg, _, _ = kh.evaluate(_inputs(lock="unheld"), _steady_state())
        self.assertIn("singleton lock not held", msg)
        msg, _, _ = kh.evaluate(_inputs(estop_age=7 * 3600), _steady_state())
        self.assertIn("ESTOP", msg)
        msg, _, _ = kh.evaluate(_inputs(estop_age=3600), _steady_state())
        self.assertEqual(msg, "")

    def test_rule4_review_waiting_over_2h(self):
        db = _inputs()["db"]
        db["review"] = [{"id": "t_r", "title": "Review me", "assignee": "coder",
                         "status": "review", "created_at": NOW - 9000,
                         "review_requested_at": NOW - 3 * 3600}]
        msg, _, _ = kh.evaluate(_inputs(db=db), _steady_state())
        self.assertIn("! review waiting 3h t_r", msg)
        db["review"][0]["review_requested_at"] = NOW - 600
        msg, _, _ = kh.evaluate(_inputs(db=db), _steady_state())
        self.assertEqual(msg, "")

    def test_rule5_silent_heartbeat_and_auto_escalate_only_for_workers(self):
        db = _inputs()["db"]
        db["running"] = [
            {"id": "t_w", "title": "worker card", "assignee": "coder", "status": "running",
             "started_at": NOW - 7200, "last_heartbeat_at": NOW - 100 * 60,
             "current_run_id": 7, "worker_pid": 123, "model_override": None, "run_started_at": NOW - 7200},
            {"id": "t_o", "title": "other card", "assignee": "engine-research", "status": "running",
             "started_at": NOW - 7200, "last_heartbeat_at": NOW - 100 * 60,
             "current_run_id": 8, "worker_pid": 124, "model_override": None, "run_started_at": NOW - 7200},
        ]
        msg, _, actions = kh.evaluate(_inputs(db=db), _steady_state())
        self.assertIn("! running silent 1h t_w @coder", msg)
        self.assertIn("t_o @engine-research", msg)
        self.assertEqual(actions, [], "auto-escalate is OFF by default")
        msg, _, actions = kh.evaluate(_inputs(db=db), _steady_state(), auto_escalate=True)
        self.assertEqual([a["task_id"] for a in actions], ["t_w"])
        self.assertEqual(actions[0]["model"], "openai/gpt-5.6-sol")
        self.assertEqual(actions[0]["provider"], "openrouter")
        # already pinned -> nothing to escalate to
        db["running"][0]["model_override"] = "openai/gpt-5.6-sol"
        _, _, actions = kh.evaluate(_inputs(db=db), _steady_state(), auto_escalate=True)
        self.assertEqual(actions, [])

    def test_rule5_protocol_violation_advances_cursor(self):
        db = _inputs()["db"]
        db["max_event_id"] = 40
        db["protocol_violations"] = [
            {"event_id": 33, "task_id": "t_pv", "created_at": NOW - 60, "run_id": 3,
             "title": "pv card", "assignee": "coder", "status": "blocked", "model_override": None},
            {"event_id": 34, "task_id": "t_np", "created_at": NOW - 60, "run_id": 4,
             "title": "not a worker", "assignee": "default", "status": "blocked", "model_override": None},
        ]
        msg, st, actions = kh.evaluate(_inputs(db=db), _steady_state(), auto_escalate=True)
        self.assertIn("! protocol violation t_pv @coder", msg)
        self.assertNotIn("t_np", msg)
        self.assertEqual(st["pv_cursor"], 34)
        self.assertEqual([a["task_id"] for a in actions], ["t_pv"])

    def test_rule6_blocked_digest_daily_and_exclusions(self):
        db = _inputs()["db"]
        db["blocked"] = [
            {"id": "t_b1", "title": "old blocked", "blocked_at": NOW - 3 * 86400, "created_at": NOW - 4 * 86400,
             "block_kind": "transient", "reason": "boom"},
            {"id": "t_b2", "title": "[DESK] HUMAN GATE", "blocked_at": NOW - 3 * 86400, "created_at": NOW - 4 * 86400,
             "block_kind": "needs_input", "reason": ""},
            {"id": "t_b3", "title": "ancient needs_input", "blocked_at": NOW - 8 * 86400, "created_at": NOW - 9 * 86400,
             "block_kind": "needs_input", "reason": ""},
            {"id": "t_b4", "title": "fresh", "blocked_at": NOW - 3600, "created_at": NOW - 3600,
             "block_kind": "transient", "reason": ""},
        ]
        msg, st, _ = kh.evaluate(_inputs(db=db), _steady_state())
        self.assertIn("blocked > 2d (daily digest, 1):", msg)
        self.assertIn("t_b1", msg)
        for skipped in ("t_b2", "t_b3", "t_b4"):
            self.assertNotIn(skipped, msg)
        self.assertEqual(st["blocked_digest_at"], NOW)
        msg2, _, _ = kh.evaluate(_inputs(db=db, now=NOW + 3600), st)
        self.assertEqual(msg2, "", "digest is once per day")

    def test_rule7_ci_red_once_per_run_then_green_again(self):
        red = {"CI": {"run_id": 9, "run_number": 120, "status": "completed", "conclusion": "failure",
                      "head_sha": "abc1234", "url": "https://example/ci/9"}}
        st = _steady_state()
        msg1, st1, _ = kh.evaluate(_inputs(github=red), st)
        self.assertIn("! CI on main failure: run #120", msg1)
        msg2, st2, _ = kh.evaluate(_inputs(github=red), st1)
        self.assertEqual(msg2, "")
        green = {"CI": {"run_id": 10, "run_number": 121, "status": "completed", "conclusion": "success",
                        "head_sha": "def5678", "url": ""}}
        msg3, st3, _ = kh.evaluate(_inputs(github=green), st2)
        self.assertIn("ok CI on main green again", msg3)
        self.assertFalse(st3["gh"]["CI"]["red"])
        # github unavailable -> keep the previous snapshot, no alert
        _, st4, _ = kh.evaluate(_inputs(github=None), st1)
        self.assertEqual(st4["gh"], st1["gh"])

    def test_rule8_done_digest_format_and_cursor(self):
        db = _inputs()["db"]
        db["max_event_id"] = 12
        db["completed_events"] = [
            {"event_id": 11, "task_id": "t_d", "created_at": NOW - 60, "run_id": 5, "title": "Did a thing",
             "assignee": "reviewer", "changes_requested": 2, "pr_number": 42, "merge_sha": "0123456789abcdef"},
        ]
        msg, st, _ = kh.evaluate(_inputs(db=db), _steady_state())
        self.assertIn("done since last digest (1):", msg)
        self.assertIn("t_d — Did a thing — PR #42 — 0123456 (changes_requested x2)", msg)
        self.assertEqual(st["done_cursor"], 11)

    def test_unavailable_inputs_carry_fingerprints_instead_of_realerting(self):
        diags = [{"task_id": "t_c", "title": "x", "status": "ready", "assignee": "coder",
                  "diagnostics": [{"kind": "k", "severity": "error", "title": "t"}]}]
        _, st1, _ = kh.evaluate(_inputs(diagnostics=diags), _steady_state())
        msg, st2, _ = kh.evaluate(_inputs(diagnostics=None, stats=None, db=None), st1)
        self.assertEqual(msg, "")
        self.assertIn("diag:t_c:k", st2["fingerprints"])
        msg3, _, _ = kh.evaluate(_inputs(diagnostics=diags), st2)
        self.assertEqual(msg3, "", "carried fingerprint must not re-alert when the input returns")

    def test_message_header_and_never_json(self):
        msg, _, _ = kh.evaluate(_inputs(lock="missing"), _steady_state())
        first = msg.splitlines()[0]
        self.assertTrue(first.startswith("kanban-health 2027-"), first)
        self.assertFalse(msg.splitlines()[-1].lstrip().startswith("{"))


def _make_board(path: Path, now: int = NOW) -> None:
    """Minimal subset of the upstream schema (kanban_db.py:1333-1480) — only
    the columns the probe touches."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
            created_at INTEGER, started_at INTEGER, last_heartbeat_at INTEGER,
            current_run_id INTEGER, worker_pid INTEGER, model_override TEXT,
            block_kind TEXT, last_failure_error TEXT);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, started_at INTEGER, metadata TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,
            kind TEXT, payload TEXT, created_at INTEGER);
    """)
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("t_run", "running card", "coder", "running", now - 9000, now - 8000, now - 7000, 1, 99, None, None, None),
        ("t_rev", "review card", "coder", "review", now - 9000, None, None, None, None, None, None, None),
        ("t_blk", "blocked card", "coder", "blocked", now - 9000, None, None, None, None, None, "transient", "err"),
        ("t_done", "done card", "reviewer", "done", now - 9000, None, None, 2, None, None, None, None),
    ])
    conn.executemany("INSERT INTO task_runs VALUES (?,?,?,?)", [
        (1, "t_run", now - 8000, None),
        (2, "t_done", now - 8000, json.dumps({"pr_number": 7, "merge_sha": "feedfacecafe"})),
    ])
    conn.executemany("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)", [
        ("t_rev", None, "review_requested", "{}", now - 8000),
        ("t_blk", None, "blocked", json.dumps({"reason": "flaky upstream"}), now - 3 * 86400),
        ("t_done", 2, "changes_requested", "{}", now - 7000),
        ("t_done", 2, "completed", "{}", now - 6000),
        ("t_run", 1, "protocol_violation", "{}", now - 100),
    ])
    conn.commit()
    conn.close()


class ReadDbTests(unittest.TestCase):
    def test_reads_all_sections_from_a_real_sqlite_board(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "kanban.db"
            _make_board(db)
            out = kh.read_db(db, done_cursor=0, pv_cursor=0)
        self.assertIsNotNone(out)
        self.assertEqual([r["id"] for r in out["running"]], ["t_run"])
        self.assertEqual(out["running"][0]["last_heartbeat_at"], NOW - 7000)
        self.assertEqual(out["review"][0]["review_requested_at"], NOW - 8000)
        self.assertEqual(out["blocked"][0]["reason"], "flaky upstream")
        self.assertEqual(out["blocked"][0]["blocked_at"], NOW - 3 * 86400)
        self.assertEqual(out["max_event_id"], 5)
        self.assertEqual(len(out["completed_events"]), 1)
        ev = out["completed_events"][0]
        self.assertEqual((ev["pr_number"], ev["merge_sha"], ev["changes_requested"]), (7, "feedfacecafe", 1))
        self.assertEqual([e["task_id"] for e in out["protocol_violations"]], ["t_run"])

    def test_missing_db_returns_none(self):
        with TemporaryDirectory() as td:
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(kh.read_db(Path(td) / "nope.db", 0, 0))

    def test_cursors_none_skip_history_queries(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "kanban.db"
            _make_board(db)
            out = kh.read_db(db, done_cursor=None, pv_cursor=None)
        self.assertEqual(out["completed_events"], [])
        self.assertEqual(out["protocol_violations"], [])


class RunEndToEndTests(unittest.TestCase):
    """``run()`` with no ``hermes`` on PATH: CLI-backed rules carry, DB-backed
    rules work, GitHub skipped, exit 0 always, stdout empty when nothing new."""

    def _run(self, argv, env_extra=None):
        out, err = io.StringIO(), io.StringIO()
        saved = dict(os.environ)
        os.environ.update(env_extra or {})
        os.environ["PATH"] = "/nonexistent-bin"
        os.environ.pop("HERMES_KANBAN_DB", None)
        saved_bin = kh.DEFAULT_HERMES_BIN
        kh.DEFAULT_HERMES_BIN = "/nonexistent-bin/hermes"
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = kh.run(argv)
        finally:
            kh.DEFAULT_HERMES_BIN = saved_bin
            os.environ.clear()
            os.environ.update(saved)
        return rc, out.getvalue(), err.getvalue()

    def test_first_run_is_silent_and_writes_state_second_run_reports_db_rules(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            db = home / "kanban.db"
            _make_board(db, now=int(time.time()))
            state = home / "kanban-health.state.json"
            env = {"HERMES_HOME": str(home)}
            rc, out, _ = self._run(["--state-file", str(state), "--db", str(db), "--no-github"], env)
            self.assertEqual(rc, 0)
            self.assertTrue(state.exists())
            st = json.loads(state.read_text())
            self.assertEqual(st["done_cursor"], 5)
            self.assertEqual(st["pv_cursor"], 5)
            # first run still reports live conditions (lock missing, stale review,
            # stale blocked digest) -- only cursors/rising-age need a second sample
            self.assertIn("kanban-health", out)
            self.assertIn("singleton lock not held", out)
            self.assertIn("review waiting", out)
            self.assertIn("blocked >", out)
            # second run: nothing new -> silent
            rc, out2, _ = self._run(["--state-file", str(state), "--db", str(db), "--no-github"], env)
            self.assertEqual(rc, 0)
            self.assertEqual(out2.strip(), "")

    def test_dry_run_does_not_write_state_and_labels_output(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            db = home / "kanban.db"
            _make_board(db, now=int(time.time()))
            state = home / "state.json"
            rc, out, _ = self._run(["--state-file", str(state), "--db", str(db), "--no-github",
                                    "--dry-run", "--auto-escalate"], {"HERMES_HOME": str(home)})
            self.assertEqual(rc, 0)
            self.assertFalse(state.exists())
            self.assertTrue(out.startswith("[dry-run]"), out)
            self.assertNotIn("escalated t_", out, "dry-run must never touch the board")

    def test_internal_error_exits_zero_and_stays_silent_on_stdout(self):
        # An unwritable state dir makes run() raise; main() is the cron entry
        # point and must swallow it (non-zero exit would page via the watchdog).
        argv = ["kanban-health", "--state-file", "/nonexistent-dir/x/state.json",
                "--db", "/nonexistent-dir/kanban.db", "--no-github"]
        out, err = io.StringIO(), io.StringIO()
        saved_argv = sys.argv
        sys.argv = argv
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = kh.main()
        finally:
            sys.argv = saved_argv
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "", "errors go to stderr only without --debug")
        self.assertIn("error", err.getvalue())


class ApplyEscalationsTests(unittest.TestCase):
    def test_uses_set_model_then_comment_via_runner(self):
        calls = []

        class P:
            returncode = 0

        def runner(cmd):
            calls.append(cmd)
            return P()

        actions = [{"task_id": "t_x", "model": "openai/gpt-5.6-sol", "provider": "openrouter",
                    "comment": "c", "fingerprint": "f", "why": "w"}]
        lines = kh.apply_escalations(actions, None, runner=runner)
        self.assertEqual(calls[0][1:], ["kanban", "set-model", "t_x", "openai/gpt-5.6-sol", "--provider", "openrouter"])
        self.assertEqual(calls[1][1:4], ["kanban", "comment", "t_x"])
        self.assertIn("escalated t_x", lines[0])


class ScriptShapeTests(unittest.TestCase):
    def test_shebang_and_contract_constants(self):
        head = SCRIPT.read_text().splitlines()[0]
        self.assertEqual(head, "#!/opt/hermes/.venv/bin/python")
        self.assertEqual(kh.DEFAULT_STATE_FILE, "/opt/data/kanban-health.state.json")
        self.assertEqual(kh.ESCALATE_MODEL, "openai/gpt-5.6-sol")
        self.assertEqual(kh.DEFAULT_WORKER_PROFILES, ("coder",))
        self.assertEqual(kh.GITHUB_REPO, "howemoney/stopsargassum")


if __name__ == "__main__":
    unittest.main()
