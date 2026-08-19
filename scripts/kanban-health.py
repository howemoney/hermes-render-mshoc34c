#!/opt/hermes/.venv/bin/python
"""Zero-LLM kanban health probe for the Hermes-on-Render SDLC board.

Runs as a `no_agent` Hermes cron job every few hours and prints ONLY when
something is new. The cron contract (verified at hermes v2026.8.18,
cron/scheduler.py:4616-4733, the ``no_agent`` branch of ``run_job``):

  * the job is created with
        hermes cron create "every 3h" --no-agent --script kanban-health.py \\
            --name kanban-health --deliver telegram
    (flags: hermes_cli/subcommands/cron.py:46-70)
  * the script must live under ``$HERMES_HOME/scripts/`` — any other path is
    rejected with "resolves outside the scripts directory"
    (cron/scheduler.py:3523-3562). The 017 boot hook copies this file from
    /opt/render-tools/scripts/ to /opt/data/scripts/ when missing.
  * it is executed with the gateway's own interpreter (``sys.executable``),
    NOT this shebang (cron/scheduler.py:3571-3601) — same venv either way.
  * stdout, stripped, is delivered verbatim to the ``--deliver`` target;
    EMPTY stdout = silent run, nothing is sent (cron/scheduler.py:4714-4723).
  * a non-zero exit (or a timeout, default 3600 s) is delivered as a
    "Cron watchdog ... script failed" alert that includes stderr
    (cron/scheduler.py:3660-3666, :4680-4697). So: on internal error this
    script exits 0 and stays silent on stdout (stderr only) unless --debug.
  * if the LAST stdout line is JSON ``{"wakeAgent": false}`` the run is
    treated as silent (cron/scheduler.py:3742-3765). We never print JSON.
  * the subprocess environment is SCRUBBED: ``GH_TOKEN`` / ``GITHUB_TOKEN``
    are on Hermes' always-strip list (tools/environments/local.py:556-559
    and :310), so the cron child never sees them even when the gateway has
    them. We therefore also read ``$HERMES_HOME/.env`` directly and, failing
    that, fall back to the baked ``gh`` CLI's own auth. No token, no GitHub
    rule — silently.
  * ``hermes pause`` (the ESTOP sentinel at ``$HERMES_HOME/ESTOP``,
    agent/estop.py:37-56) halts CRON dispatch too (cron/scheduler.py:6805-6808),
    so rule 3c ("ESTOP older than 6 h") can only ever fire from a manual
    ``hermes cron run`` or a shell invocation. It is kept because that is
    exactly when an operator is poking around wondering why nothing moves.

Inputs (all best-effort; a missing input disables the rules that need it):

  * ``hermes kanban diagnostics --json`` — list of
    ``{task_id,title,status,assignee,diagnostics:[{kind,severity,title,...}]}``
    (hermes_cli/kanban.py:1935-2030, kanban_diagnostics.Diagnostic.to_dict).
  * ``hermes kanban stats --json`` — ``{by_status,by_assignee,
    oldest_ready_age_seconds,now}`` (hermes_cli/kanban_db.py:11256-11289).
  * direct read-only SQLite reads of the board DB: tables ``tasks``,
    ``task_events`` (id, task_id, run_id, kind, payload, created_at) and
    ``task_runs`` (metadata JSON carries the completion metadata)
    (hermes_cli/kanban_db.py:1333-1480). DB path resolution mirrors upstream
    ``kanban_db.kanban_db_path()`` (kanban_db.py:713-735): ``HERMES_KANBAN_DB``
    env, else ``<root>/kanban.db`` for the default board where ``<root>`` is
    ``HERMES_KANBAN_HOME`` or ``HERMES_HOME`` (``/opt/data`` here), or
    ``<root>/kanban/boards/<slug>/kanban.db`` when ``<root>/kanban/current``
    names another board. NOTE: the default board's DB is ``/opt/data/kanban.db``,
    not ``/opt/data/kanban/kanban.db``.
  * ``flock`` probe of ``<root>/kanban/.dispatcher.lock``
    (gateway/kanban_watchers.py:1240, taken with LOCK_EX|LOCK_NB in
    gateway/status.py:780-793). If WE can take it, nobody holds it, i.e. no
    gateway dispatcher is alive.
  * ``$HERMES_HOME/ESTOP`` sentinel mtime.
  * GitHub Actions: latest run of the ``CI`` and ``Deploy`` workflows on
    ``main`` of howemoney/stopsargassum.

Rules (plan section E). Each alert carries a fingerprint kept in the state
file with ``first_seen``; a fingerprint already present is NOT re-sent unless
its severity escalated. Fingerprints that stop matching are dropped, so a
condition that clears and recurs alerts again.

  1. diagnostic at severity >= error — new or escalated (warning->error->critical)
  2. ``stranded_in_ready`` diagnostic at any severity
  3. dispatcher not ticking: (a) ready > 0 and oldest_ready_age > 1800 s AND
     rising since the previous run — i.e. the oldest ready card did not move
     for a whole interval (needs two samples, so never on the first run; and
     not when the board is simply capped by kanban.max_in_progress);
     (b) the singleton lock is unheld; (c) ESTOP sentinel older than 6 h
  4. card in ``review`` for > 2 h (review is by definition unclaimed; age is
     measured from the latest ``review_requested`` event)
  5. running card whose heartbeat (``COALESCE(last_heartbeat_at, run
     started_at)``) is older than 90 min, or a ``protocol_violation`` event
     on a worker-profile card since the last run. With ``--auto-escalate``
     (default OFF) such a card gets ``hermes kanban set-model <id>
     openai/gpt-5.6-sol --provider openrouter`` + a ``kanban comment``.
  6. blocked > 48 h — once a day, as a digest; excludes cards titled
     ``HUMAN GATE`` and ``needs_input`` cards older than 7 d (both are
     waiting on a human who already knows)
  7. latest ``CI`` / ``Deploy`` run on main is red (once per run id; a
     "green again" line when it recovers)
  8. done digest: every ``completed`` event past the saved cursor as
     ``t_id — title — PR #N — merge_sha (changes_requested xK)``. The very
     first run only records the cursor (no 51-line history dump).

State file: ``/opt/data/kanban-health.state.json`` (``--state-file``).
Written atomically. ``--dry-run`` prints what WOULD be sent and leaves the
state file and the board untouched.

Structure: everything that talks to the world lives in ``collect_inputs`` /
``apply_escalations``; ``evaluate`` is a pure function of
``(inputs, state, now)`` so tests feed dict fixtures. Keep it that way.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_VERSION = 1
DEFAULT_HERMES_HOME = "/opt/data"
DEFAULT_STATE_FILE = "/opt/data/kanban-health.state.json"
DEFAULT_HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
GITHUB_REPO = "howemoney/stopsargassum"
GITHUB_WORKFLOWS = ("CI", "Deploy")
ESCALATE_MODEL = "openai/gpt-5.6-sol"
ESCALATE_PROVIDER = "openrouter"
DEFAULT_WORKER_PROFILES = ("coder",)

READY_STALE_SECONDS = 1800          # rule 3a
ESTOP_STALE_SECONDS = 6 * 3600      # rule 3c
REVIEW_STALE_SECONDS = 2 * 3600     # rule 4
HEARTBEAT_STALE_SECONDS = 90 * 60   # rule 5
BLOCKED_STALE_SECONDS = 48 * 3600   # rule 6
NEEDS_INPUT_GRACE_SECONDS = 7 * 86400
BLOCKED_DIGEST_INTERVAL = 86400
SEVERITY_RANK = {"warning": 1, "error": 2, "critical": 3}
GREEN_CONCLUSIONS = {"success", "skipped", "neutral"}

# Event kinds that put a card into ``blocked`` (kanban_db.py:4490-4494 lists
# the same family as "durable phase" events). Used for rule 6 age.
BLOCKING_EVENT_KINDS = (
    "blocked", "block_loop_detected", "gave_up", "crashed", "timed_out",
    "spawn_failed", "protocol_violation", "rate_limited", "stale",
)

HERMES_CLI_TIMEOUT = 180
GH_TIMEOUT = 60


def _log(msg: str) -> None:
    print(f"kanban-health: {msg}", file=sys.stderr)


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 3600:
        return f"{max(s // 60, 0)}m"
    if s < 2 * 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _short(text: Any, n: int = 70) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "fingerprints": {},
        "done_cursor": None,
        "pv_cursor": None,
        "last_oldest_ready_age": None,
        "blocked_digest_at": None,
        "gh": {},
        "last_run": None,
    }


def load_state(path: Path) -> dict:
    state = empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, ValueError) as exc:
        _log(f"state file unreadable ({exc}); starting fresh")
        return state
    if isinstance(raw, dict):
        for k in state:
            if k in raw:
                state[k] = raw[k]
    if not isinstance(state.get("fingerprints"), dict):
        state["fingerprints"] = {}
    if not isinstance(state.get("gh"), dict):
        state["gh"] = {}
    state["version"] = STATE_VERSION
    return state


def save_state(path: Path, state: dict) -> None:
    """Atomic write: temp file in the same dir + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kanban-health.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Path resolution (mirrors upstream; imports are optional)
# ---------------------------------------------------------------------------

def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "").strip() or DEFAULT_HERMES_HOME).expanduser()


def kanban_root() -> Path:
    """``kanban_db.kanban_home()`` (kanban_db.py:566-586) without the import:
    HERMES_KANBAN_HOME wins, else the Hermes root. On this deployment
    HERMES_HOME is /opt/data and is not a profile dir, so root == HERMES_HOME."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return hermes_home()


def resolve_db_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    # Prefer upstream's resolver when importable (the cron runs inside the
    # Hermes venv, where hermes_cli is a normal package).
    try:
        from hermes_cli import kanban_db as _kb  # type: ignore

        return Path(_kb.kanban_db_path())
    except Exception:
        pass
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    root = kanban_root()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if not board:
        try:
            board = (root / "kanban" / "current").read_text(encoding="utf-8").strip()
        except OSError:
            board = ""
    if board and board != "default":
        return root / "kanban" / "boards" / board / "kanban.db"
    return root / "kanban.db"  # kanban_db.py:733-734


def dispatcher_lock_path() -> Path:
    return kanban_root() / "kanban" / ".dispatcher.lock"  # kanban_watchers.py:1240


def estop_path() -> Path:
    return hermes_home() / "ESTOP"  # agent/estop.py:37,54-56


def hermes_bin() -> str:
    return shutil.which("hermes") or DEFAULT_HERMES_BIN


# ---------------------------------------------------------------------------
# Collectors (impure)
# ---------------------------------------------------------------------------

def _subprocess_env(db_path: Optional[Path]) -> dict:
    env = dict(os.environ)
    if db_path is not None:
        # Keep the CLI on the same DB we read directly.
        env["HERMES_KANBAN_DB"] = str(db_path)
    return env


def run_hermes_json(args: list[str], db_path: Optional[Path] = None) -> Any:
    """Run ``hermes <args>`` and parse its stdout as JSON. Returns None on any
    failure (missing binary, non-zero exit, unparsable output)."""
    cmd = [hermes_bin(), *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=HERMES_CLI_TIMEOUT,
            env=_subprocess_env(db_path),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"{' '.join(args)} failed to run: {exc}")
        return None
    if proc.returncode != 0:
        _log(f"{' '.join(args)} exited {proc.returncode}: {_short(proc.stderr, 200)}")
        return None
    out = proc.stdout or ""
    # Tolerate stray non-JSON preamble lines (warnings printed to stdout).
    start = min([i for i in (out.find("["), out.find("{")) if i >= 0] or [-1])
    if start < 0:
        _log(f"{' '.join(args)} printed no JSON")
        return None
    try:
        return json.loads(out[start:])
    except ValueError as exc:
        _log(f"{' '.join(args)} JSON parse failed: {exc}")
        return None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _try_rows(conn: sqlite3.Connection, what: str, sql: str, params: tuple = ()) -> list[dict]:
    """One failing query (e.g. a column a pre-upgrade DB lacks) must not take
    every other rule down with it — log and return no rows."""
    try:
        return _rows(conn, sql, params)
    except sqlite3.Error as exc:
        _log(f"db query '{what}' failed: {exc}")
        return []


def _json_or_empty(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def open_db_readonly(path: Path) -> sqlite3.Connection:
    """Open read-only. The board runs in WAL mode; a ``mode=ro`` open needs the
    -shm/-wal siblings to be creatable, which holds for the ``hermes`` user on
    /opt/data. Fall back to a plain open (we only SELECT) if ro fails."""
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.execute("SELECT 1 FROM tasks LIMIT 1")
        return conn
    except sqlite3.Error:
        return sqlite3.connect(str(path), timeout=30)


def read_db(path: Path, done_cursor: Optional[int], pv_cursor: Optional[int]) -> Optional[dict]:
    """Pull everything the rules need from the board DB in one pass.

    Returns None when the DB is missing/unreadable (rules 4/5/6/8 then skip).
    """
    if not path.exists():
        _log(f"kanban db not found at {path}")
        return None
    try:
        conn = open_db_readonly(path)
    except sqlite3.Error as exc:
        _log(f"kanban db open failed: {exc}")
        return None
    try:
        out: dict[str, Any] = {"db_path": str(path)}

        out["running"] = _try_rows(conn, "running", """
            SELECT t.id, t.title, t.assignee, t.status, t.started_at,
                   t.last_heartbeat_at, t.current_run_id, t.worker_pid,
                   t.model_override,
                   COALESCE(r.started_at, t.started_at) AS run_started_at
              FROM tasks t
              LEFT JOIN task_runs r ON r.id = t.current_run_id
             WHERE t.status = 'running'
        """)

        out["review"] = _try_rows(conn, "review", """
            SELECT t.id, t.title, t.assignee, t.status, t.created_at,
                   (SELECT MAX(e.created_at) FROM task_events e
                     WHERE e.task_id = t.id AND e.kind = 'review_requested') AS review_requested_at
              FROM tasks t
             WHERE t.status = 'review'
        """)

        placeholders = ",".join("?" for _ in BLOCKING_EVENT_KINDS)
        out["blocked"] = _try_rows(conn, "blocked", f"""
            SELECT t.id, t.title, t.assignee, t.status, t.created_at,
                   t.block_kind, t.last_failure_error,
                   (SELECT MAX(e.created_at) FROM task_events e
                     WHERE e.task_id = t.id AND e.kind IN ({placeholders})) AS blocked_at,
                   (SELECT e.payload FROM task_events e
                     WHERE e.task_id = t.id AND e.kind = 'blocked'
                     ORDER BY e.id DESC LIMIT 1) AS blocked_payload
              FROM tasks t
             WHERE t.status = 'blocked'
        """, tuple(BLOCKING_EVENT_KINDS))
        for row in out["blocked"]:
            payload = _json_or_empty(row.pop("blocked_payload", None))
            row["reason"] = payload.get("reason") or row.get("last_failure_error") or ""

        max_row = conn.execute("SELECT MAX(id) AS m FROM task_events").fetchone()
        out["max_event_id"] = int(max_row[0]) if max_row and max_row[0] is not None else 0

        out["completed_events"] = []
        if done_cursor is not None:
            out["completed_events"] = _try_rows(conn, "completed", """
                SELECT e.id AS event_id, e.task_id, e.created_at, e.run_id,
                       t.title, t.assignee, r.metadata AS run_metadata,
                       (SELECT COUNT(*) FROM task_events c
                         WHERE c.task_id = e.task_id AND c.kind = 'changes_requested') AS changes_requested
                  FROM task_events e
                  LEFT JOIN tasks t ON t.id = e.task_id
                  LEFT JOIN task_runs r ON r.id = e.run_id
                 WHERE e.kind = 'completed' AND e.id > ?
                 ORDER BY e.id
            """, (int(done_cursor),))
            for row in out["completed_events"]:
                md = _json_or_empty(row.pop("run_metadata", None))
                row["pr_number"] = md.get("pr_number")
                row["merge_sha"] = md.get("merge_sha")

        out["protocol_violations"] = []
        if pv_cursor is not None:
            out["protocol_violations"] = _try_rows(conn, "protocol_violations", """
                SELECT e.id AS event_id, e.task_id, e.created_at, e.run_id,
                       t.title, t.assignee, t.status, t.model_override
                  FROM task_events e
                  LEFT JOIN tasks t ON t.id = e.task_id
                 WHERE e.kind = 'protocol_violation' AND e.id > ?
                 ORDER BY e.id
            """, (int(pv_cursor),))
        return out
    except sqlite3.Error as exc:
        _log(f"kanban db read failed: {exc}")
        return None
    finally:
        conn.close()


def probe_dispatcher_lock(path: Path) -> str:
    """'held' | 'unheld' | 'missing' | 'unavailable'.

    Same primitive the gateway uses (fcntl.flock LOCK_EX|LOCK_NB,
    gateway/status.py:790). flock(2) works on a read-only fd, so we never
    create or modify the file. If the lock is granted we release it at once.
    """
    if not path.exists():
        return "missing"
    try:
        import fcntl
    except ImportError:
        return "unavailable"
    try:
        fh = open(str(path), "r")
    except OSError:
        return "unavailable"
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "held"
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return "unheld"
    finally:
        fh.close()


def estop_age(path: Path, now: int) -> Optional[int]:
    try:
        st = path.stat()
    except OSError:
        return None
    return max(now - int(st.st_mtime), 0)


def read_max_in_progress(config_path: Path) -> Optional[int]:
    """kanban.max_in_progress from config.yaml, if PyYAML is around."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, Exception):
        return None
    try:
        val = (data.get("kanban") or {}).get("max_in_progress")
        return int(val) if val is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def parse_dotenv(path: Path) -> dict:
    """Minimal KEY=VALUE parser for $HERMES_HOME/.env (quotes + `export `)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def find_github_token() -> Optional[str]:
    """Process env first (manual runs), then $HERMES_HOME/.env — the cron child
    never inherits GH_TOKEN (see module docstring). Never logged."""
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    dotenv = parse_dotenv(hermes_home() / ".env")
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = (dotenv.get(key) or "").strip()
        if val:
            return val
    return None


def fetch_github_runs(repo: str, token: Optional[str]) -> Optional[list]:
    """Latest workflow runs on main. Token -> REST API; no token -> the baked
    ``gh`` CLI with whatever auth it has; neither -> None (rule 7 skipped)."""
    path = f"repos/{repo}/actions/runs?branch=main&per_page=30&exclude_pull_requests=true"
    if token:
        req = urllib.request.Request(
            f"https://api.github.com/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "kanban-health",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=GH_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log(f"github api failed: {type(exc).__name__}")
            return None
        return data.get("workflow_runs") if isinstance(data, dict) else None
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        proc = subprocess.run(
            [gh, "api", path], capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data.get("workflow_runs") if isinstance(data, dict) else None


def latest_runs_by_workflow(runs: Optional[list], names=GITHUB_WORKFLOWS) -> dict:
    """{name: {run_id, run_number, status, conclusion, url, head_sha}} for the
    newest run per workflow name (the API returns newest first)."""
    out: dict[str, dict] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        name = run.get("name")
        if name not in names or name in out:
            continue
        out[name] = {
            "run_id": run.get("id"),
            "run_number": run.get("run_number"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "url": run.get("html_url"),
            "head_sha": (run.get("head_sha") or "")[:7],
        }
    return out


def collect_inputs(args: argparse.Namespace, state: dict, now: int) -> dict:
    db_path = resolve_db_path(args.db)
    inputs: dict[str, Any] = {
        "now": now,
        "db_path": str(db_path),
        "worker_profiles": list(args.worker_profiles),
    }
    inputs["diagnostics"] = run_hermes_json(["kanban", "diagnostics", "--json"], db_path)
    inputs["stats"] = run_hermes_json(["kanban", "stats", "--json"], db_path)
    inputs["db"] = read_db(db_path, state.get("done_cursor"), state.get("pv_cursor"))
    inputs["lock"] = probe_dispatcher_lock(dispatcher_lock_path())
    inputs["estop_age"] = estop_age(estop_path(), now)
    inputs["max_in_progress"] = read_max_in_progress(hermes_home() / "config.yaml")
    if args.no_github:
        inputs["github"] = None
    else:
        inputs["github"] = latest_runs_by_workflow(
            fetch_github_runs(GITHUB_REPO, find_github_token())
        )
    return inputs


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------

class Report:
    """Accumulates output lines + fingerprints + escalation actions."""

    def __init__(self, state: dict, now: int):
        self.now = now
        self.prev_fps: dict = dict(state.get("fingerprints") or {})
        self.active_fps: dict = {}
        self.alerts: list[str] = []
        self.done_lines: list[str] = []
        self.blocked_lines: list[str] = []
        self.actions: list[dict] = []

    def carry(self, prefix: str) -> None:
        """An input was unavailable this run: keep its fingerprints alive so
        the same condition does not re-alert when the input comes back."""
        for fp, entry in self.prev_fps.items():
            if fp.startswith(prefix) and fp not in self.active_fps:
                self.active_fps[fp] = entry

    def fire(self, fp: str, line: str, severity: Optional[str] = None, **extra) -> bool:
        """Register an active fingerprint; return True when it is NEW or its
        severity escalated (= the line should be sent)."""
        prev = self.prev_fps.get(fp)
        entry = {
            "first_seen": (prev or {}).get("first_seen", self.now),
            "last_seen": self.now,
            "severity": severity,
            "escalated": bool((prev or {}).get("escalated")),
        }
        entry.update(extra)
        self.active_fps[fp] = entry
        if prev is None:
            self.alerts.append(line)
            return True
        if severity and SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(prev.get("severity") or "", 0):
            self.alerts.append(f"{line} (escalated from {prev.get('severity')})")
            return True
        return False


def _diag_line(task: dict, diag: dict) -> str:
    who = f" @{task.get('assignee')}" if task.get("assignee") else ""
    return (
        f"! diag {diag.get('severity')} {task.get('task_id')} {diag.get('kind')}"
        f" — {_short(diag.get('title') or diag.get('detail'))}"
        f" [{task.get('status') or '?'}{who}] {_short(task.get('title'), 50)}"
    )


def rule_diagnostics(inputs: dict, rep: Report) -> None:
    """Rules 1 + 2."""
    diags = inputs.get("diagnostics")
    if not isinstance(diags, list):
        rep.carry("diag:")
        return
    for task in diags:
        if not isinstance(task, dict):
            continue
        for diag in task.get("diagnostics") or []:
            if not isinstance(diag, dict):
                continue
            sev = diag.get("severity") or "warning"
            kind = diag.get("kind") or "?"
            if SEVERITY_RANK.get(sev, 0) >= SEVERITY_RANK["error"] or kind == "stranded_in_ready":
                rep.fire(f"diag:{task.get('task_id')}:{kind}", _diag_line(task, diag), severity=sev)


def rule_dispatcher(inputs: dict, state: dict, rep: Report) -> Optional[int]:
    """Rule 3. Returns the oldest_ready_age to persist for the next run."""
    stats = inputs.get("stats")
    if not isinstance(stats, dict):
        rep.carry("dispatcher:ready-stale")
        stats = {}
    by_status = stats.get("by_status") or {}
    ready = int(by_status.get("ready", 0) or 0)
    running = int(by_status.get("running", 0) or 0)
    age = stats.get("oldest_ready_age_seconds")
    age = int(age) if isinstance(age, (int, float)) else None
    prev_age = state.get("last_oldest_ready_age")
    cap = inputs.get("max_in_progress")
    capped = isinstance(cap, int) and cap > 0 and running >= cap

    if ready > 0 and age is not None and age > READY_STALE_SECONDS and not capped:
        # "Rising" = the oldest ready card has NOT moved since the previous
        # run: its age grew by (about) the whole interval. A first run has
        # no baseline and only records the age — two samples are needed.
        last_run = state.get("last_run")
        if prev_age is None:
            rising = False
        elif isinstance(last_run, int) and last_run < rep.now:
            rising = (age - int(prev_age)) >= (rep.now - last_run) - 300
        else:
            rising = age > int(prev_age)
        if rising:
            rep.fire(
                "dispatcher:ready-stale",
                f"! dispatcher: {ready} ready, oldest waiting {fmt_age(age)} and rising"
                f" — is the in-gateway dispatcher ticking? (hermes kanban dispatch --dry-run)",
            )
    lock = inputs.get("lock")
    if lock in ("unheld", "missing"):
        rep.fire(
            "dispatcher:lock-unheld",
            "! dispatcher: singleton lock not held (no gateway dispatcher alive,"
            f" lock {lock}) — check gateway log for 'holding singleton dispatcher lock'",
        )
    est = inputs.get("estop_age")
    if isinstance(est, int) and est > ESTOP_STALE_SECONDS:
        rep.fire(
            "dispatcher:estop",
            f"! paused: ESTOP sentinel is {fmt_age(est)} old — `hermes resume` when the hold is over",
        )
    return age


def rule_review_stale(inputs: dict, rep: Report) -> None:
    """Rule 4."""
    db = inputs.get("db")
    if not isinstance(db, dict):
        rep.carry("review-stale:")
        return
    now = inputs["now"]
    for row in db.get("review") or []:
        since = row.get("review_requested_at") or row.get("created_at")
        if since is None:
            continue
        age = now - int(since)
        if age > REVIEW_STALE_SECONDS:
            rep.fire(
                f"review-stale:{row['id']}",
                f"! review waiting {fmt_age(age)} {row['id']} — {_short(row.get('title'))}"
                f" (is a reviewer profile free? max_in_progress_per_profile=1)",
            )


def rule_workers(inputs: dict, rep: Report, auto_escalate: bool) -> Optional[int]:
    """Rule 5: stale heartbeat on running cards + new protocol violations on
    worker-profile cards. Returns the protocol-violation cursor to persist."""
    db = inputs.get("db")
    if not isinstance(db, dict):
        rep.carry("heartbeat:")
        return None
    now = inputs["now"]
    workers = set(inputs.get("worker_profiles") or DEFAULT_WORKER_PROFILES)

    for row in db.get("running") or []:
        last = row.get("last_heartbeat_at") or row.get("run_started_at") or row.get("started_at")
        if last is None:
            continue
        age = now - int(last)
        if age <= HEARTBEAT_STALE_SECONDS:
            continue
        fp = f"heartbeat:{row['id']}:{row.get('current_run_id')}"
        line = (
            f"! running silent {fmt_age(age)} {row['id']} @{row.get('assignee')}"
            f" pid {row.get('worker_pid')} — {_short(row.get('title'))}"
        )
        fired = rep.fire(fp, line)
        if fired and auto_escalate and row.get("assignee") in workers:
            _queue_escalation(rep, row["id"], f"heartbeat silent {fmt_age(age)}",
                              row.get("model_override"), fp)

    pv_rows = db.get("protocol_violations") or []
    new_cursor = None
    for ev in pv_rows:
        new_cursor = max(new_cursor or 0, int(ev["event_id"]))
        if ev.get("assignee") not in workers:
            continue
        line = (
            f"! protocol violation {ev['task_id']} @{ev.get('assignee')} run {ev.get('run_id')}"
            f" — exited without kanban_complete/kanban_block — {_short(ev.get('title'))}"
        )
        rep.alerts.append(line)
        if auto_escalate:
            _queue_escalation(rep, ev["task_id"], "protocol violation",
                              ev.get("model_override"), f"pv:{ev['task_id']}:{ev['event_id']}")
    return new_cursor


def _queue_escalation(rep: Report, task_id: str, why: str, current_model: Optional[str], fp: str) -> None:
    if current_model == ESCALATE_MODEL:
        return  # already pinned; nothing to escalate to
    if any(a["task_id"] == task_id for a in rep.actions):
        return
    rep.actions.append({
        "task_id": task_id,
        "model": ESCALATE_MODEL,
        "provider": ESCALATE_PROVIDER,
        "comment": (
            f"kanban-health: auto-escalated model to {ESCALATE_MODEL} after {why}."
            " Takes effect on the next dispatch of this card."
        ),
        "fingerprint": fp,
        "why": why,
    })


def rule_blocked_digest(inputs: dict, state: dict, rep: Report) -> bool:
    """Rule 6. Returns True when a digest was emitted (caller stamps the day)."""
    db = inputs.get("db") or {}
    now = inputs["now"]
    last = state.get("blocked_digest_at")
    if last is not None and now - int(last) < BLOCKED_DIGEST_INTERVAL:
        return False
    lines = []
    for row in db.get("blocked") or []:
        title = row.get("title") or ""
        if "HUMAN GATE" in title.upper():
            continue
        since = row.get("blocked_at") or row.get("created_at")
        if since is None:
            continue
        age = now - int(since)
        if age <= BLOCKED_STALE_SECONDS:
            continue
        kind = row.get("block_kind") or "untyped"
        if kind == "needs_input" and age > NEEDS_INPUT_GRACE_SECONDS:
            continue
        lines.append(
            f"  {row['id']} {fmt_age(age)} [{kind}] {_short(title, 50)}"
            + (f" — {_short(row.get('reason'), 60)}" if row.get("reason") else "")
        )
    if not lines:
        return False
    rep.blocked_lines = [f"blocked > {fmt_age(BLOCKED_STALE_SECONDS)} (daily digest, {len(lines)}):"] + lines
    return True


def rule_github(inputs: dict, state: dict, rep: Report) -> dict:
    """Rule 7. Returns the gh snapshot to persist."""
    gh = inputs.get("github")
    prev_gh = state.get("gh") or {}
    snapshot: dict = {}
    if not isinstance(gh, dict):
        rep.carry("gh:")
        return prev_gh  # no data this run: keep what we knew
    for name in GITHUB_WORKFLOWS:
        run = gh.get(name)
        if not isinstance(run, dict) or run.get("run_id") is None:
            continue
        red = run.get("status") == "completed" and (run.get("conclusion") or "") not in GREEN_CONCLUSIONS
        snapshot[name] = {"run_id": run.get("run_id"), "conclusion": run.get("conclusion"), "red": red}
        if red:
            rep.fire(
                f"gh:{name}:{run.get('run_id')}",
                f"! {name} on main {run.get('conclusion')}: run #{run.get('run_number')}"
                f" {run.get('head_sha')} {run.get('url') or ''}".rstrip(),
            )
        elif prev_gh.get(name, {}).get("red") and run.get("status") == "completed":
            rep.alerts.append(
                f"ok {name} on main green again: run #{run.get('run_number')} {run.get('head_sha')}"
            )
    return snapshot


def rule_done_digest(inputs: dict, state: dict, rep: Report) -> Optional[int]:
    """Rule 8. Returns the done cursor to persist."""
    db = inputs.get("db")
    if not isinstance(db, dict):
        return state.get("done_cursor")
    max_id = int(db.get("max_event_id") or 0)
    cursor = state.get("done_cursor")
    if cursor is None:
        return max_id  # first run: record, don't dump history
    events = db.get("completed_events") or []
    if not events:
        return cursor
    lines = []
    newest = int(cursor)
    for ev in events:
        newest = max(newest, int(ev["event_id"]))
        pr = f" — PR #{ev['pr_number']}" if ev.get("pr_number") else ""
        sha = f" — {str(ev['merge_sha'])[:7]}" if ev.get("merge_sha") else ""
        cr = int(ev.get("changes_requested") or 0)
        crs = f" (changes_requested x{cr})" if cr else ""
        lines.append(f"  {ev['task_id']} — {_short(ev.get('title'), 60)}{pr}{sha}{crs}")
    rep.done_lines = [f"done since last digest ({len(lines)}):"] + lines
    return newest


def evaluate(inputs: dict, state: dict, *, auto_escalate: bool = False) -> tuple[str, dict, list]:
    """Pure: (inputs, state) -> (message, new_state, escalation actions).

    ``message`` is "" when nothing new happened (= silent cron run).
    """
    now = int(inputs.get("now") or time.time())
    rep = Report(state, now)
    new_state = json.loads(json.dumps(state))  # deep copy, JSON-safe
    new_state.setdefault("fingerprints", {})

    rule_diagnostics(inputs, rep)
    new_state["last_oldest_ready_age"] = rule_dispatcher(inputs, state, rep)
    rule_review_stale(inputs, rep)
    pv_cursor = rule_workers(inputs, rep, auto_escalate)
    if rule_blocked_digest(inputs, state, rep):
        new_state["blocked_digest_at"] = now
    new_state["gh"] = rule_github(inputs, state, rep)
    new_state["done_cursor"] = rule_done_digest(inputs, state, rep)

    db = inputs.get("db")
    if isinstance(db, dict):
        if state.get("pv_cursor") is None:
            new_state["pv_cursor"] = int(db.get("max_event_id") or 0)
        elif pv_cursor is not None:
            new_state["pv_cursor"] = max(int(state["pv_cursor"]), pv_cursor)
        else:
            new_state["pv_cursor"] = state.get("pv_cursor")

    new_state["fingerprints"] = rep.active_fps
    new_state["last_run"] = now
    new_state["version"] = STATE_VERSION

    parts: list[str] = []
    parts.extend(rep.alerts)
    if rep.done_lines:
        parts.extend(rep.done_lines)
    if rep.blocked_lines:
        parts.extend(rep.blocked_lines)
    if not parts:
        return "", new_state, rep.actions
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"kanban-health {stamp}"
    return "\n".join([header, *parts]), new_state, rep.actions


# ---------------------------------------------------------------------------
# Escalation (impure)
# ---------------------------------------------------------------------------

def apply_escalations(actions: list, db_path: Optional[Path], runner: Optional[Callable] = None) -> list[str]:
    """Run ``hermes kanban set-model`` + ``hermes kanban comment`` per action
    (hermes_cli/kanban.py:482-496, :567-573). Returns result lines."""
    run = runner or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=HERMES_CLI_TIMEOUT, env=_subprocess_env(db_path),
    ))
    results: list[str] = []
    for act in actions:
        tid = act["task_id"]
        try:
            p1 = run([hermes_bin(), "kanban", "set-model", tid, act["model"], "--provider", act["provider"]])
            if getattr(p1, "returncode", 1) != 0:
                results.append(f"  escalation failed for {tid}: set-model rc={getattr(p1, 'returncode', '?')}")
                continue
            run([hermes_bin(), "kanban", "comment", tid, act["comment"], "--author", "kanban-health"])
            results.append(f"  escalated {tid} -> {act['model']} ({act['why']})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append(f"  escalation failed for {tid}: {type(exc).__name__}")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kanban-health",
        description="Zero-LLM kanban board health probe (prints only when something is new).",
    )
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    p.add_argument("--db", default=None, help="kanban.db path (default: upstream resolution)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent; do not write state or touch the board")
    p.add_argument("--auto-escalate", action="store_true",
                   help=f"rule 5: pin stuck worker cards to {ESCALATE_MODEL} via set-model + comment")
    p.add_argument("--worker-profiles", default=",".join(DEFAULT_WORKER_PROFILES),
                   help="comma-separated assignee profiles treated as workers (rule 5)")
    p.add_argument("--no-github", action="store_true", help="skip rule 7 entirely")
    p.add_argument("--debug", action="store_true",
                   help="on internal error print one line to stdout (default: stderr only, exit 0)")
    return p


def run(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.worker_profiles = tuple(x.strip() for x in str(args.worker_profiles).split(",") if x.strip())
    now = int(time.time())
    state_path = Path(args.state_file).expanduser()
    state = load_state(state_path)
    inputs = collect_inputs(args, state, now)
    message, new_state, actions = evaluate(inputs, state, auto_escalate=args.auto_escalate)

    extra: list[str] = []
    if actions:
        if args.dry_run:
            extra = [f"  would escalate {a['task_id']} -> {a['model']} ({a['why']})" for a in actions]
        else:
            extra = apply_escalations(actions, Path(inputs["db_path"]))
        for fp in (a["fingerprint"] for a in actions):
            if fp in new_state.get("fingerprints", {}):
                new_state["fingerprints"][fp]["escalated"] = True

    if extra:
        message = "\n".join([message or f"kanban-health {datetime.fromtimestamp(now, tz=timezone.utc):%Y-%m-%d %H:%M UTC}", *extra])

    if args.dry_run:
        if message:
            print("[dry-run] would send:\n" + message)
        else:
            print("[dry-run] nothing to send")
        return 0
    save_state(state_path, new_state)
    if message:
        print(message)
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # never fail the cron; a non-zero exit would page
        _log(f"error {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        if "--debug" in sys.argv[1:]:
            print(f"kanban-health: error {type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
