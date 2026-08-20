---
name: kanban-sdlc-reviewer
description: Use when you are the Kanban `reviewer` profile spawned from the review lane for a stopsargassum card, to verify the implementer's PR against the house rules, run the GATE, wait for CI, squash-merge with `gh`, and close the card with `kanban_complete` (or return it with `kanban_request_changes`).
version: 1.0.0
author: Howe Agency
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [kanban, sdlc, review, merge, github, stopsargassum]
    category: devops
    requires_toolsets: [kanban, terminal, file]
    related_skills: [sdlc-review, kanban-sdlc-worker, merge-reconciler]
environments:
  - kanban
---

# Kanban SDLC reviewer (stopsargassum / Howe Agency)

```
+--------------------------------------------------------------------------+
| TERMINAL-ACTION CONTRACT                                                 |
| This run ends with EXACTLY ONE of:                                       |
|   kanban_complete(summary=..., metadata=...)        after a green merge  |
|   kanban_request_changes(reason=...)                correctable defects  |
|   kanban_block(kind="needs_input", reason=...)      human-only decision  |
| You never edit implementation files, never push main, never force-push,  |
| never merge red, and never stop without one of the calls above.          |
+--------------------------------------------------------------------------+
```

This is the **house overlay** on upstream `sdlc-review`, which the dispatcher
force-loads on every review-lane spawn. `sdlc-review` owns the verdict
logic (approve / request changes / escalate), the per-round **review lenses**
(round 1 artifact, round 2 execution, round 3+ contract) and the pitfalls
list — read it and apply it; nothing here replaces it. This skill adds what
`sdlc-review` leaves open for our repo: the rule audit, the GATE as merge
policy, the CI wait, the squash-merge mechanics, and the conflict path.

## Role gate

- `$HERMES_PROFILE` is `reviewer` AND `kanban_show()` shows a
  `review_requested` handoff for this card (claimed from `review`) → continue.
- `$HERMES_PROFILE` is `coder` (or the card has no `review_requested`
  handoff and is a normal implementation card) → wrong skill: load
  `skill_view(name="kanban-sdlc-worker")` (coder) or
  `kanban_block(kind="needs_input", reason="assigned to reviewer without a review_requested handoff; reassign to coder")` (reviewer).
- Card title starts with `[RECONCILE]` / `merge-reconciler` is loaded → you
  are the neutral reconciler on a `ready`-lane card, not the reviewer: follow
  `merge-reconciler`, finish with `kanban_complete`, still never touch main.

Your workspace is the **implementer's worktree** on the implementer's
`wt/<id>` branch (same `workspace_path`, same branch — upstream reuses it).
Treat it as read-mostly: fetch/merge/update-branch are fine, edits are not.

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

## Procedure

### 1. Orient

`kanban_show()`. Read the latest `review_requested` handoff: `summary` and
`metadata` (`pr_url`, `pr_number`, `branch`, `head_sha`, `base_sha`,
`changed_files`, `tests_run`, `gate`, `migrations_claimed`, `new_worker`,
`hotspot`, `residual_risk`, `round`). Count `changes_requested` entries in
prior attempts: **round = count + 1** → pick the `sdlc-review` lens for that
round and lead with it. Missing `pr_number`/`head_sha` in metadata is itself
a defect → step 9b with a precise ask (the worker skill specifies the keys).

### 2. Verify the artifact is what the handoff claims

```bash
cd "$HERMES_KANBAN_WORKSPACE"; N=<pr_number>; BR=$(git branch --show-current)
git fetch origin                                   # NEVER --prune (see step 9a)
git rev-parse HEAD                                 # must == metadata.head_sha
gh pr view "$N" --json number,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,isDraft,state
```

Required: `state=OPEN`, `baseRefName=main`, `headRefName=$BR`,
`headRefOid == HEAD == metadata.head_sha`, `isDraft=false`. If the remote
branch is ahead of your HEAD (worker pushed after requesting review):
`git merge --ff-only origin/$BR` and re-check — if `headRefOid` still
differs from the metadata, the handoff is stale → `kanban_request_changes`
with "re-request review with current head_sha". `mergeable=CONFLICTING` or
`mergeStateStatus=DIRTY` → step 8.

### 3. Rule audit (CODE_SHAPE.md / CLAUDE.md; each miss is a concrete finding)

```bash
git diff --stat origin/main...HEAD
git log --format='%H %s' origin/main..HEAD          # every subject ends with "(t_<id>)"
git diff --name-only origin/main...HEAD | grep -E '^migrations/' || echo "no migrations"
git for-each-ref --format='%(refname)' refs/remotes/origin \
  | while read r; do git ls-tree -r --name-only "$r" -- migrations; done \
  | grep -oE '(^|/)[0-9]{4}_' | tr -d '/_' | sort | uniq -d          # duplicates across ALL remotes = blocker
git diff --name-only origin/main...HEAD | grep -E '0011_' && echo "0011 touched: blocker"
git diff origin/main...HEAD -- migrations | grep -iE '^\+.*(DROP|ALTER .*DROP|DELETE FROM|TRUNCATE)' && echo "non-additive: needs approval note"
git diff --name-only --diff-filter=A origin/main...HEAD | grep -E '^workers/[^/]+/' | cut -d/ -f2 | sort -u   # NEW worker dirs?
git diff --name-only origin/main...HEAD | grep -E '^(\.github/workflows/deploy\.yml|package\.json|\.env\.example)$'  # the required trio when one appears
git show origin/main:workers/engine-status/src/index.js | grep -n 'url.pathname ===' > /tmp/routes-main.txt
git diff origin/main...HEAD -- workers/engine-status/src/index.js | grep -E '^\+.*url\.pathname ===' # each path must NOT already be in /tmp/routes-main.txt
gh pr view "$N" --json title --jq .title                                                               # "<card title> [t_<id>]"
gh pr view "$N" --json body --jq .body | grep -cE '^## (Card|Summary|Acceptance|Tests run|Migration claims|Smoke|Deploy wiring|Hotspots|Residual|Process)'   # expect 10
```

Checks: PR title `<card title> [t_<id>]`; all ten template headings present;
migration numbers unique across every remote branch; `0011_*` untouched;
additive-only unless the migration header carries an approval note; a new
Worker updates deploy.yml paths-filter + package.json test chain +
.env.example; no engine-status route re-declared; tenant isolation
(`agency` vs `stopsargassum`); nothing staged from the denylist
(`.output/ .wrangler/ node_modules/ .env* .hermes* *.log .worktrees/`).
Then do the lens-appropriate review from `sdlc-review` (criteria → evidence,
tests assert behavior, scope drift, prior-round items landed).

### 4. Run the full GATE — always; it is the merge policy

Load `skill_view(name="kanban-sdlc-worker", file_path="references/gate.md")`
and run the script exactly as the worker does (background + heartbeats).
`CHANGED_FILES`/`CHANGED_WORKERS` come from `git diff --name-only
origin/main...HEAD`. `gate: "pass"` in the handoff is a claim; your run is
the evidence. Red → step 9b with the failing step and log excerpt.

### 5. Wait for CI (the PR's `CI` workflow on `pull_request`)

```bash
for i in 1 2 3 4 5 6 7 8 9 10; do
  timeout 120 gh pr checks "$N" --watch --fail-fast; rc=$?
  [ $rc -ne 124 ] && break
done; echo "checks rc=$rc"
```

Call `kanban_heartbeat(note="waiting on CI for PR #N, loop $i")` between
loops (one `terminal` call per loop, ≤120 s each, ≤10 loops ≈ 20 min). Exit
`0` → green. "no checks reported" on the first loops → CI has not started;
wait one more loop. Any other non-zero → red:

```bash
gh run list --branch "$BR" --limit 5 --json databaseId,name,conclusion,url,headSha
gh run view <databaseId> --log-failed | tail -n 80
```

Summarize the failing job/step in ≤10 lines → step 9b. Never merge red,
never re-run CI to "see if it passes" more than once for an obviously flaky
network step.

### 6. If `main` moved under the PR

`gh pr view "$N" --json mergeStateStatus` → `BEHIND`:

```bash
gh pr update-branch "$N"                 # default = merge origin/main into the PR branch; NEVER --rebase
git fetch origin && git merge --ff-only "origin/$BR" && HEAD_SHA=$(git rev-parse HEAD)
```

Then re-wait CI (step 5) on the new head. If the update merge touched any
path in `changed_files`, re-run the GATE too. `update-branch` reports
conflicts (or status flips to `DIRTY`) → step 8.

### 7. Squash-merge

```bash
git fetch origin                                     # immediately before merging, still no --prune
HEAD_SHA=$(git rev-parse HEAD)                       # == gh pr view $N --json headRefOid
gh pr merge "$N" -R howemoney/stopsargassum --squash --delete-branch \
  --match-head-commit "$HEAD_SHA" \
  --subject "<type>(<scope>): <subject> (t_<id>) (#$N)" \
  --body "Kanban: t_<id> round <n>
Tests: <condensed from the PR>
Migrations: <none | claimed NNNN>
Conflict-resolution: <none | ...>
Reviewed-by: reviewer (gate pass, CI green)"
```

Flags verified against `gh pr merge --help` (gh 2.9x): `--squash`,
`--delete-branch`, `--match-head-commit`, `--subject`, `--body`, `-R`. Do
NOT use `--auto` (we want the merge to happen now, under our own
`--match-head-commit`), `--admin`, `--merge`, or `--rebase`.

**Why `-R howemoney/stopsargassum` is mandatory here:** `gh` only tries to
delete the *local* branch when it can resolve the repo from the current
directory (`CanDeleteLocalBranch = !flags.Changed("repo")`, upstream
`pkg/cmd/pr/merge/merge.go`). From inside a linked worktree that local
delete runs `git checkout main` — which fails because `main` is checked out
in the anchor — and `gh` then exits non-zero **without deleting the remote
branch**, after the merge already happened. With `-R` the local step is
skipped and the remote `wt/<id>` is deleted through the API, which leaves
the local `refs/remotes/origin/wt/<id>` entry in place (that entry is what
lets upstream's worktree cleanup see the commits as pushed).

Verify:

```bash
git fetch origin                                     # no --prune
MERGE_SHA=$(gh pr view "$N" --json mergeCommit --jq .mergeCommit.oid)
[ "$(git rev-parse origin/main)" = "$MERGE_SHA" ] && echo "origin/main == merge commit" || echo "main advanced past merge (ok if newer commits landed): $(git rev-parse origin/main)"
git merge-base --is-ancestor "$MERGE_SHA" origin/main && echo "merge on main: ok"
gh pr view "$N" --json state,mergedAt --jq '{state,mergedAt}'
```

`gh pr merge` exits non-zero with the PR still `OPEN` → read the message: a
head mismatch means a new push landed (go to step 2); anything else is a
finding for step 9b or, if it is a GitHub/permission wall, step 9c.

### 8. Conflict path (PR `DIRTY` / update-branch cannot merge)

Do not resolve it yourself (you lack the implementer's intent and the
`sdlc-review` role separation forbids edits). Create the reconciler card,
gate the implementation card on it, and return the card:

```
recon = kanban_create(
  title="[RECONCILE] wt/t_<id> x origin/main — <card title>",
  assignee="reviewer",
  skills=["merge-reconciler"],
  workspace_kind="dir",
  workspace_path="<absolute path of this worktree, i.e. $HERMES_KANBAN_WORKSPACE>",
  body="Merge origin/main into wt/t_<id> (PR #N) as a neutral third party per merge-reconciler.\n"
       "Sides: wt/t_<id> = <card intent, from kanban_show>; origin/main = <commits since base_sha, from git log>.\n"
       "Conflicting paths: <list>. Run the GATE (skill kanban-sdlc-worker references/gate.md) after resolving, "
       "push wt/t_<id> (never main, never force), kanban_complete with the merged head sha.",
  tenant="<same tenant as the card>",
  max_runtime_seconds=5400
)
kanban_link(parent_id=recon["id"], child_id=$HERMES_KANBAN_TASK)
kanban_comment(task_id=$HERMES_KANBAN_TASK, body="hotspot: <path> — conflicts with origin/main; reconciler card <recon id>")
kanban_request_changes(reason="origin/main conflicts in <paths>; reconciler card <recon id> will merge main into wt/t_<id>; then re-run GATE and re-request review with the new head_sha")
```

`kanban_link` makes the implementation card wait in `todo` until the
reconciler completes, then it promotes to `ready` and the coder resumes.
Use the id returned by `kanban_create` — never a remembered one.

### 9. Verdict (exactly one)

**9a. Approve — right after the merge, before anything else**

```
kanban_complete(
  summary="Reviewed and merged PR #N as <merge_sha>: <what was verified in one sentence>.",
  metadata={
    "review_outcome": "approved",
    "pr_number": N,
    "merge_sha": "<MERGE_SHA>",
    "ci_run_url": "<url from gh run list / checks>",
    "gate": "pass",
    "round": <n>,
    "reviewer_checks": ["rule audit", "GATE", "CI", "squash-merge", "origin/main verified"]
  }
)
```

Do **not** `git fetch --prune`, `git remote prune`, or `git worktree
remove/prune` between the branch delete and `kanban_complete`: upstream's
`_cleanup_worktree_workspace` fires on completion and only removes the
worktree when every commit on HEAD is reachable from a remote-tracking ref —
after the remote `wt/<id>` delete, the local `refs/remotes/origin/wt/<id>`
is the only one left, and a prune turns a clean close into a leaked
worktree. Let upstream do the cleanup.

**9b. Request changes** — concrete, reproducible findings first
(`kanban_comment(task_id=..., body="Changes requested:\n1. ...")`), then
`kanban_request_changes(reason="<summary of required corrections>")`. Never
a PR URL in the comment (it arms the 24 h `active_pr` respawn guard on the
coder's next spawn; the PR number is enough).

**9c. Escalate** — only human-only decisions or external walls (missing
`GH_TOKEN` scope, repo setting, product decision):
`kanban_block(kind="needs_input", reason="escalation: <decision or prerequisite>")`.

### 10. Never

- Never edit implementation files in the worktree (role separation;
  `sdlc-review` pitfall "Reviewer implementation").
- Never push `main`/`master`, never `--force`/`-f`, never `--no-verify`.
- Never merge red, `--admin`, `--auto`, or without `--match-head-commit`.
- Never merge without `git fetch origin` immediately before.
- Never prune between the branch delete and `kanban_complete`.
- Never `kanban_complete` without a merge (a "looks fine" without a merged
  PR is rubber-stamping; the card is done when `origin/main` has it).

## Quick self-check before the terminal call

- [ ] Round computed; `sdlc-review` lens for that round applied.
- [ ] `HEAD == headRefOid == metadata.head_sha`, base `main`, PR open.
- [ ] Rule audit done (migrations unique + 0011 untouched, wiring trio,
      routes, commit subjects, PR template).
- [ ] GATE run by you: green. CI: green on the merged head.
- [ ] Merge via `gh pr merge -R … --squash --delete-branch --match-head-commit`,
      `origin/main` verified.
- [ ] `kanban_complete` with `merge_sha`, and no prune in between — or one of
      `kanban_request_changes` / `kanban_block(kind="needs_input")`.
