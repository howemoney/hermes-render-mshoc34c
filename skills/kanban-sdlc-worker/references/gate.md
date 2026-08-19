# GATE — the merge policy, run by the worker before review and by the reviewer before merge

The GATE is the exact check set that `.github/workflows/ci.yml` runs on
`pull_request` (typecheck, test, lint, format, fresh-D1 migration apply,
build) plus the per-worker harnesses and the CODE_SHAPE hook. Running it
locally first means CI is a confirmation, not a discovery — CI minutes are
the scarce resource on this repo.

Rules that never bend:

- Run ALL of it, in this order, every round. A "small" change still runs the
  whole gate (format and migrations are where "small" changes break).
- Never `.skip()`, delete, comment out, or loosen a harness or a lint rule to
  go green. If a harness is stale, fix the harness in the same PR and list
  it under "Tests" in the PR body.
- Red means not ready. Red after your best fix → `kanban_block`, not review.

## The script

Write it verbatim to `/tmp/gate-$HERMES_KANBAN_TASK.sh` (no edits — the two
`CHANGED_*` values are passed as environment variables when you run it),
then run it in the background (section below).

```bash
#!/usr/bin/env bash
# GATE for stopsargassum — mirrors ci.yml + worker harnesses + CODE_SHAPE hook.
# Exit 0 == green. Every step logs a banner so the log is readable after the fact.
set -u
cd "${HERMES_KANBAN_WORKSPACE:?}" || exit 2

# Passed in by the caller (see "Finding CHANGED_FILES" below), space-separated.
CHANGED_FILES="${CHANGED_FILES:-}"         # e.g. "workers/engine-status/src/index.js migrations/0024_x.sql"
CHANGED_WORKERS="${CHANGED_WORKERS:-}"     # e.g. "engine-status source-videos"

export npm_config_cache=/opt/data/.npm-cache   # persistent disk; survives redeploys, shared by all workers
export CI=1 FORCE_COLOR=0 GIT_TERMINAL_PROMPT=0

step() { printf '\n===== GATE STEP: %s =====\n' "$1"; }
fail() { printf '\nGATE_RESULT=FAIL step=%s\n' "$1"; exit 1; }

step "npm ci";            npm ci --prefer-offline --no-audit --no-fund            || fail "npm-ci"
step "typecheck";         npm run typecheck                                       || fail "typecheck"
step "test";              npm test                                                || fail "test"
step "lint";              npm run lint                                            || fail "lint"
step "format:check";      npm run format:check                                    || fail "format"
step "d1 migrations (fresh local)"
npx wrangler d1 migrations apply stopsargassum --local -c wrangler.d1.toml        || fail "migrations"
step "build";             npm run build                                           || fail "build"

# Every harness of every worker you touched (CI only runs the root `npm test`
# chain; the per-worker harnesses are the real regression net).
for w in $CHANGED_WORKERS; do
  for t in workers/"$w"/test-*.mjs; do
    [ -e "$t" ] || continue
    step "harness $t"; node "$t"                                                 || fail "harness:$t"
  done
done

# CODE_SHAPE hook (migration collisions, filename shape, main-branch writes).
if [ -n "$CHANGED_FILES" ]; then
  step "check-code-shape"
  CLAUDE_FILE_PATHS="$CHANGED_FILES" node .claude/hooks/check-code-shape.mjs     || fail "code-shape"
fi

printf '\nGATE_RESULT=PASS\n'
```

## Running it under the Hermes terminal tool

Foreground `terminal` calls are capped at 600 s (`FOREGROUND_MAX_TIMEOUT`
in upstream `tools/terminal_tool.py`); a full gate is 10-25 minutes. So:

1. `terminal(command="CHANGED_FILES='<files>' CHANGED_WORKERS='<workers>' bash /tmp/gate-$ID.sh > /tmp/gate-$ID.log 2>&1; echo EXIT=$?", background=true, notify_on_complete=true, workdir=$HERMES_KANBAN_WORKSPACE)`
   — note the returned `session_id`.
2. Loop: `kanban_heartbeat(note="gate running: <last step seen>")`, then
   `process(action="wait", session_id=<id>, timeout=300)`. Repeat until it
   exits (or the completion notification arrives).
3. `tail -n 60 /tmp/gate-$ID.log` and look for the last line:
   `GATE_RESULT=PASS` → green; `GATE_RESULT=FAIL step=<name>` → read that
   step's section of the log, fix, re-run the whole script.

Keep the log: the reviewer and the PR body cite `gate: pass` and the commands
that ran; `tail` the relevant section into the PR body's "Tests run".

## Finding CHANGED_FILES / CHANGED_WORKERS

```bash
git fetch origin                                  # no --prune
git diff --name-only origin/main...HEAD
git diff --name-only origin/main...HEAD | sed -n 's#^workers/\([^/]*\)/.*#\1#p' | sort -u | tr '\n' ' '
```

## Why no `--prune` (and why it matters to you)

Upstream removes a finished card's worktree automatically
(`_cleanup_worktree_workspace` in `hermes_cli/kanban_db.py`) only when the
tree is clean AND every commit on `HEAD` is reachable from some
`refs/remotes/*` ref (`cli._worktree_has_unpushed_commits` = `git log HEAD
--not --remotes`). After the reviewer squash-merges and deletes the remote
`wt/<id>` branch, the only remote-tracking ref that still covers your
commits is the local `refs/remotes/origin/wt/<id>` entry. A `git fetch
--prune` (from any worktree — they share the repo) deletes that entry, the
commits look "unpushed", the cleanup preserves the worktree, and
`/opt/data/work/stopsargassum/.worktrees/` fills up. So: `git fetch origin`,
never `--prune`, never `git remote prune`, never `fetch.prune=true`.

## Staging denylist (step 7 of the protocol)

Never stage: `.output/` `.wrangler/` `node_modules/` `.env*` `.hermes*`
`*.log` `.worktrees/` `migrations/engine/0011_*`. `npm ci` and `npm run
build` write into `.output/`, `.wrangler/`, and `node_modules/` — those are
gitignored, but `git add -A` on a misconfigured ignore has leaked them
before. Stage by explicit path.
