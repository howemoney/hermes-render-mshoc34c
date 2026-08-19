# PR body template + creation commands

The PR body is the reviewer's evidence sheet and the squash-merge's audit
trail. Every heading below stays, even when the answer is "none" — an
absent heading reads as "the worker did not check".

## Body (write to `/tmp/pr-$HERMES_KANBAN_TASK.md`)

```markdown
## Card
t_<id> — <card title> (tenant: <agency|stopsargassum>, round <n>)

## Summary
<2-5 lines: what changed and why; the smallest-diff rationale>

## Acceptance criteria → evidence
| Criterion (verbatim from the card) | Evidence (file:line, test name, command output) |
|---|---|
| <criterion 1> | <evidence> |
| <criterion 2> | <evidence> |

## Tests run (GATE, local)
- npm run typecheck — pass
- npm test — pass
- npm run lint — pass
- npm run format:check — pass
- npx wrangler d1 migrations apply stopsargassum --local -c wrangler.d1.toml — pass
- npm run build — pass
- node workers/<w>/test-*.mjs — <list each, pass>
- check-code-shape.mjs — pass
<harness fixed in this PR, if any, and why it was stale>

## Migration claims
none | claimed NNNN_<name>.sql (max across all remote branches was MMMM at origin/main <sha>); additive-only; 0011_* untouched

## Smoke evidence
<what you actually exercised: handler driven against the node:sqlite D1 shim, curl of the local route, screenshot path, or "n/a: docs-only">

## Deploy wiring
new Worker: no | yes → deploy.yml paths-filter ✔, package.json test chain ✔, .env.example ✔

## Hotspots
none | `hotspot: <path> — <reason>` (mirrors the kanban comments)

## Residual risk
<one or two lines, or "none known">

## Process
- Branch wt/t_<id> cut from origin/main <base sha>; merged origin/main <sha> before push.
- No direct push to main. Squash-merge by the `reviewer` profile after CI.
- Kanban: t_<id> round <n>
```

## Create with `gh` (preferred)

```bash
ID=$HERMES_KANBAN_TASK; BR=$(git branch --show-current)
gh pr list --head "$BR" --state open --json number,url --jq '.[0]'      # reuse if non-empty
gh pr create --base main --head "$BR" \
  --title "<card title> [$ID]" \
  --body-file /tmp/pr-$ID.md
gh pr view --json number,url,headRefOid --jq '{number,url,headRefOid}'   # values for request_review metadata
```

`gh` authenticates from `GH_TOKEN` in the worker env (inherited from the
gateway). Never print the token; `gh auth status` is the only allowed
diagnostic.

## Fallback with `curl` (only if `gh` is missing or broken for a non-auth reason)

```bash
ID=$HERMES_KANBAN_TASK; BR=$(git branch --show-current)
python3 - "$BR" "<card title> [$ID]" /tmp/pr-$ID.md > /tmp/pr-$ID.json <<'PY'
import json, sys
head, title, body_path = sys.argv[1:4]
print(json.dumps({"title": title, "head": head, "base": "main",
                  "body": open(body_path, encoding="utf-8").read()}))
PY
# existing open PR for this head?
curl -sS -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/howemoney/stopsargassum/pulls?state=open&head=howemoney:$BR" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["html_url"] if d else "")'
# create
curl -sS -X POST -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/howemoney/stopsargassum/pulls --data @/tmp/pr-$ID.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("number"), d.get("html_url"), d.get("message",""))'
```

A 401/403 from either path is a `kanban_block(kind="capability", ...)`
(token missing or lacking `pull_requests: write`), not something to retry.
