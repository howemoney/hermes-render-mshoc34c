---
name: kanban-sdlc-worker
description: Use when you are a Kanban implementation worker (profile `coder`) on a stopsargassum card, to take the card from a fresh worktree through the GATE, a pushed `wt/<id>` branch, an open PR, and `kanban_request_review(reviewer="reviewer")`, never merging and never pushing `main`.
version: 1.0.0
author: Howe Agency
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [kanban, sdlc, git, github, worktree, stopsargassum]
    category: devops
    requires_toolsets: [kanban, terminal, file]
    related_skills: [kanban-sdlc-reviewer, sdlc-review, merge-reconciler]
environments:
  - kanban
---

# Kanban SDLC worker (stopsargassum / Howe Agency)

```
+--------------------------------------------------------------------------+
| TERMINAL-ACTION CONTRACT                                                 |
|                                                                          |
| This run ends with EXACTLY ONE of:                                       |
|   kanban_request_review(summary=..., reviewer="reviewer", metadata=...)  |
|   kanban_block(kind=..., reason=...)                                     |
|                                                                          |
| Printing a summary and stopping is a PROTOCOL VIOLATION: the dispatcher  |
| records "worker exited cleanly without a lifecycle call", counts a       |
| failure, and the card bounces. You NEVER call kanban_complete (that is   |
| the reviewer's verdict), NEVER call clarify (headless), NEVER merge, and |
| NEVER push main. If you are out of time or stuck, kanban_block.          |
+--------------------------------------------------------------------------+
```

This skill is the house protocol layered on top of the built-in "Kanban task
execution protocol" in your system prompt. Where the built-in text says
"finish with the review model encoded by the task graph", for a `coder` card
that means: **push a branch, open a PR, `kanban_request_review`** — even when
`kanban_show()` lists child cards waiting on yours: this board never
pre-creates a separate review card, so same-card review is the encoded model
and the reviewer's `kanban_complete` is what releases those children.
Everything else in the built-in protocol (heartbeats, hotspot comments,
`kanban_create` for follow-ups, no `clarify`, no `hermes kanban` shell-outs)
still applies.

## Role gate

Before anything else, check who you are:

- `$HERMES_PROFILE` is `reviewer`, **or** `kanban_show()` shows a
  `review_requested` handoff that no later `changes_requested` has answered
  (i.e. the card sits in the `review` column and you were spawned to judge
  it) → STOP. Wrong skill. Load `skill_view(name="kanban-sdlc-reviewer")`
  and follow that instead.
- `$HERMES_PROFILE` is `coder` (or any non-reviewer profile on a `ready`
  card with a git worktree workspace) → continue.

## GitHub access: use the MCP tools, not `gh`

**`gh` is NOT authenticated inside your sandbox and cannot be made so.**
Hermes hides credential-shaped env vars (`*_TOKEN`, `*_API_KEY`, `*_SECRET`,
`*_KEY`) from tool subprocesses, so `gh auth status` reports *"You are not
logged into any GitHub hosts"* even though the container itself holds a valid
`GITHUB_TOKEN`. Do not try to work around this: do not read the token out of
a credential helper, a config file, `/proc`, or the environment, and never
paste a token into a command.

Use these instead:

| Need | Use |
|---|---|
| push your branch | plain `git push` — the `git-credential-hermes-gateway` helper answers for it |
| open / update a PR | the **`github` MCP tools** (`create_pull_request`, `update_pull_request`, …) |
| read PR state, checks, CI logs | the **`github` MCP tools** (`get_pull_request`, `get_pull_request_status`, `list_workflow_runs`, `get_job_logs`) |
| merge a PR | the **`github` MCP tool** `merge_pull_request` with `merge_method: "squash"` |

If the `github` MCP tools are not in your tool list, stop and
`kanban_block(kind="capability", reason="github MCP server not available to
this profile; gh is sandboxed — cannot open/inspect/merge a PR")`. That is a
real infrastructure gap, not something to improvise around.

## Protocol

Do the steps in order. Commands assume `bash`; run them with the `terminal`
tool from `$HERMES_KANBAN_WORKSPACE` (pass `workdir`).

### 1. Orient

`kanban_show()` (no args). Read: title, body, acceptance criteria, the
parent handoffs, and **prior attempts**. Count the `changes_requested`
entries: round = count + 1. On round >= 2 the reviewer's latest
`changes_requested` reason is the FIRST item of your plan — address every
bullet of it before new work. Note every comment that starts with
`hotspot:`; do not pile more changes onto those paths without saying so in
the PR body.

### 2. Enter the worktree, verify branch + identity

```bash
cd "$HERMES_KANBAN_WORKSPACE"
git rev-parse --show-toplevel
git branch --show-current                 # must be ${HERMES_KANBAN_BRANCH:-wt/$HERMES_KANBAN_TASK}
git config user.name; git config user.email   # expect "Howe Agency Bot" (set by the boot hook)
```

If the branch is `main`/`master` → `kanban_block(kind="capability", reason="worktree is on main; dispatcher did not cut wt/<id>")`.
If identity is empty: `git config user.name "Howe Agency Bot"` and
`git config user.email "howe-agency-bot@users.noreply.github.com"` (repo-local).

**Explicit override of the repo's CLAUDE.md rule "`git checkout -b <slug> main`":**
your branch already exists and is already checked out — do NOT create
another. Wherever CLAUDE.md / CODE_SHAPE.md says "main", read it as
`origin/main` (the local `main` checkout belongs to the anchor repo, is
shared by every worker, and is NEVER touched from a worktree).

### 3. Sync the base (fetch, never prune; merge, never rebase)

```bash
git fetch origin                                   # NO --prune (see "Why no prune" in references/gate.md)
OWN=$(git rev-list --count origin/main..HEAD)      # commits on HEAD that origin/main lacks
REMOTE=$(git ls-remote --heads origin "$(git branch --show-current)" | wc -l)
if [ "$OWN" = 0 ] && [ "$REMOTE" = 0 ]; then
  git reset --hard origin/main                     # round-1 fresh card: jump to the real tip
else
  git merge --no-edit origin/main                  # pushed branch: merge, never rebase, never force
fi
```

The worktree is cut from the anchor's local HEAD, which can trail
`origin/main` — that is why the reset/merge above is not optional. If
`OWN>0` but `git log origin/main..HEAD --format='%s'` shows subjects without
your `(t_<id>)` tag, those commits are anchor drift, not yours: reset to
`origin/main` (after `git stash` if you have uncommitted edits).

Merge conflict → `git merge --abort`, then
`kanban_comment(task_id=$HERMES_KANBAN_TASK, body="hotspot: <path> — conflicts with origin/main (<their sha>)")`
for each conflicting path, then
`kanban_block(kind="needs_input", reason="merge conflict with origin/main in <paths>; needs merge-reconciler before this card can proceed")`.

### 4. Pre-flight rules (CODE_SHAPE.md, enforced by the reviewer)

- **Migration number** = max across ALL remote branches' `migrations/**` + 1
  (globally unique, never reuse, never renumber someone else's):
  ```bash
  git for-each-ref --format='%(refname)' refs/remotes/origin \
    | while read r; do git ls-tree -r --name-only "$r" -- migrations; done \
    | grep -oE '(^|/)[0-9]{4}_' | tr -d '/_' | sort -un | tail -1      # today: 0023 -> claim 0024
  ```
  Never touch any `0011_*` file (double-claimed; a dedicated hygiene card
  owns it). Additive-only SQL. A card that needs a NEW engine table (or any
  non-additive change) → `kanban_block(kind="needs_input", ...)` — those
  need operator approval per CLAUDE.md section 4.
- **engine-status route shadowing**: before adding a route, check
  `git show origin/main:workers/engine-status/src/index.js | grep -n 'url.pathname ==='`
  and do not re-declare a path that already exists (a second handler for
  `/desk`, `/evidence-desk`, etc. silently shadows the live one).
- **New Worker** → update all three: `.github/workflows/deploy.yml`
  paths-filter, root `package.json` `test` chain, `.env.example`.
- **Tenant sanity**: `agency` (building blocks) vs `stopsargassum`
  (engine-run). Never write the site DB; never write engine tables outside
  `engine/.../d1_api.py`'s allowlist.

### 5. Implement

Smallest diff that satisfies the acceptance criteria. Test the handler, not
the file: drive the real handler against a `node:sqlite` D1 shim (pattern:
`workers/engine-status/test-desk-smoke.mjs`). Bug fix → write the failing
test first, then fix. Call `kanban_heartbeat(note=...)` at least every
20 minutes and immediately before any long command. Do not edit files
outside the worktree.

### 6. GATE (mandatory, unaltered)

Read `skill_view(name="kanban-sdlc-worker", file_path="references/gate.md")`
and run the script exactly as written. Summary of what it runs:

```bash
export npm_config_cache=/opt/data/.npm-cache
npm ci --prefer-offline --no-audit --no-fund
npm run typecheck && npm test && npm run lint && npm run format:check \
  && npx wrangler d1 migrations apply stopsargassum --local -c wrangler.d1.toml \
  && npm run build
for t in workers/<each changed worker>/test-*.mjs; do node "$t"; done
CLAUDE_FILE_PATHS="<changed files, space-separated>" node .claude/hooks/check-code-shape.mjs
```

The `terminal` tool caps foreground commands at 600 s, so run the gate with
`background=true, notify_on_complete=true`, then `process(action="wait",
session_id=..., timeout=300)` in a loop with `kanban_heartbeat` between
waits. Green means every command exited 0. **Never** skip, weaken, `.skip()`
or delete a harness to make it pass; if a harness is wrong, fix it and say so
under "Tests" in the PR. Red after a genuine fix attempt → back to step 5;
still red at the time budget → step 13.

### 7. Stage explicit paths only

`git add <path> <path> ...` — never `git add -A` / `git add .`. Refuse to
stage anything matching:

```
.output/  .wrangler/  node_modules/  .env*  .hermes*  *.log  .worktrees/  migrations/engine/0011_*
```

`git status --porcelain` must show only intended paths before committing.

### 8. Commit

```
<type>(<scope>): <subject> (t_<id>)

<why, 1-5 lines>

Kanban: t_<id> round <n>
Tests: <commands that ran green, comma-separated>
Migrations: none | claimed NNNN (max was MMMM at <origin/main sha>)
Conflict-resolution: none | merged origin/main <sha>, resolved <paths>
```

`<type>` in feat|fix|chore|docs|test|refactor; `<scope>` = worker or area.
`(t_<id>)` is literally `($HERMES_KANBAN_TASK)`, e.g. `(t_c4b82e0d)` — the
reviewer greps for it and the squash subject keeps it. One logical commit
per round is fine; do not squash already-pushed history.

### 9. Push

```bash
git push -u origin "$(git branch --show-current)"
```

The repo's pre-push hook rejects any `main`/`master` update — if you see
that rejection you are on the wrong branch; stop and go to step 2. Non-fast-
forward → `git fetch origin && git merge --no-edit origin/$(git branch
--show-current)` and push again. Never `--force`; `--force-with-lease` only
if the reviewer explicitly asked for a history rewrite in `changes_requested`.

### 10. Open (or reuse) the PR

```bash
BR=$(git branch --show-current); ID=$HERMES_KANBAN_TASK
gh pr list --head "$BR" --state open --json number,url --jq '.[0]'
```

If that prints a PR → reuse it (push already updated it; edit the body with
`gh pr edit <n> --body-file /tmp/pr-$ID.md` if the evidence changed).
Otherwise write the body from
`skill_view(name="kanban-sdlc-worker", file_path="references/pr-template.md")`
to `/tmp/pr-$ID.md` and:

```bash
gh pr create --base main --head "$BR" --title "<card title> [$ID]" --body-file /tmp/pr-$ID.md
```

`gh` missing or failing with a non-auth error → curl fallback (also in
`references/pr-template.md`). Auth failure (401/403) → `kanban_block(kind="capability", reason="GH_TOKEN missing or lacks pull_requests:write")`.

### 11. Hand off for review — and stop

```
kanban_request_review(
  summary="<1-2 sentences: what changed, how verified>",
  reviewer="reviewer",
  metadata={
    "pr_url": "https://github.com/howemoney/stopsargassum/pull/<n>",
    "pr_number": <n>,
    "branch": "wt/t_<id>",
    "head_sha": "<git rev-parse HEAD>",
    "base_sha": "<git rev-parse origin/main>",
    "changed_files": ["workers/engine-status/src/index.js", "..."],
    "tests_run": ["npm run typecheck", "npm test", "npm run lint",
                  "npm run format:check", "wrangler d1 migrations apply --local",
                  "npm run build", "node workers/engine-status/test-desk-smoke.mjs",
                  "check-code-shape.mjs"],
    "gate": "pass",
    "migrations_claimed": [],
    "new_worker": false,
    "hotspot": [],
    "residual_risk": "<one line or empty>",
    "round": 1
  }
)
```

**NEVER put the PR URL in a `kanban_comment`.** The dispatcher treats a
GitHub `…/pull/N` URL in any comment newer than 24 h as an "active PR"
respawn guard (upstream `hermes_cli/kanban_db.py`: `_RESPAWN_GUARD_PR_URL_RE`
/ `_RESPAWN_GUARD_PR_WINDOW`). It is skipped for the review lane, but it
fires for the `ready` lane — so after the reviewer returns the card with
`changes_requested`, a PR URL in a comment stalls the respawn for a day. The
PR URL lives in `kanban_request_review` metadata only. After the call
returns, end your turn. Do not keep working.

### 12. Failure → exactly one `kanban_block`

| Situation | `kind` | Before blocking |
|---|---|---|
| Spec ambiguity, needs operator approval (new engine table, non-additive migration, human gate), merge conflict | `needs_input` | `kanban_comment` the precise question; for conflicts add `hotspot:` lines |
| Missing token/tool/permission (`GH_TOKEN`, `gh`, `npm` registry auth, wrangler login) | `capability` | name the missing thing, never its value |
| 5xx / network / registry flake still failing after 2 retries; out of time | `transient` | push whatever is committed (WIP commit is fine) so nothing is lost |
| Needs another card's output first | `dependency` | **only after** `kanban_create(title=..., assignee=<right profile>, body=...)` the blocker and `kanban_link(parent_id=<new id>, child_id=$HERMES_KANBAN_TASK)`; without the link the card is re-promoted forever |

Never `kanban_complete`. Never `clarify`. One block per run.

### 13. Time budget

Cards run with `--max-runtime 2h` (dispatcher SIGTERMs at the cap and the
run counts as `timed_out`). Track elapsed time from your first tool call. At
**~90 minutes without a green gate**: commit and `git push` what you have
(branch only), then
`kanban_block(kind="transient", reason="out of time; pushed <sha> on wt/t_<id>; remaining: <bullets>")`.
A clean block with state pushed beats a SIGTERM with work lost.

## Quick self-check before the terminal call

- [ ] Branch is `wt/<id>`, never main; identity is Howe Agency Bot.
- [ ] Fetched (no prune) and merged/reset onto `origin/main` this run.
- [ ] GATE ran in full and was green (or you are blocking, not reviewing).
- [ ] Only intended paths staged; none from the denylist.
- [ ] Commit subject ends with `(t_<id>)` and carries the four trailers.
- [ ] Pushed to `origin wt/<id>`; PR exists with the template body.
- [ ] `kanban_request_review(..., reviewer="reviewer", metadata={...})` with
      `pr_url`, `head_sha`, `gate` — and no PR URL in any comment.
