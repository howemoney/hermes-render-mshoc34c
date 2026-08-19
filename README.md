# Hermes Agent on Render, pre-baked with Render tools

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the self-improving AI agent from Nous Research) on Render as a single Docker web service, **already wired up to your Render account**. The image extends the upstream Hermes container with:

- The [Render MCP server](https://render.com/docs/mcp-server) registered in `config.yaml` at boot, so MCP tools appear as `mcp_render_list_services`, `mcp_render_get_metrics`, `mcp_render_list_logs`, etc. The agent gets the full MCP tool catalog that your API key can use.
- The official [render-oss/skills](https://github.com/render-oss/skills) bundle (22 Render skills) pinned at a commit and exposed via `skills.external_dirs`.
- A `render-on-hermes` overlay skill that tells the agent the MCP server is already wired up, that the CLI is not installed, and how to behave when an upstream skill expects either.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy-template/api/github/start?template_repo=hermes-render)

The Hermes release and the skills commit are both pinned in the `Dockerfile` for reproducible deploys. All Hermes state lives on a persistent disk so upgrades stay non-destructive, and the dashboard at the service URL is the primary setup surface.

> **Use at your own risk:** The agent can use every Render MCP tool allowed by `RENDER_MCP_API_KEY`, including tools that mutate resources. Lock down dashboard access and use the least-privileged Render account you can.

## Architecture

```
                            ┌──────────────────────────────────────────────┐
                            │ Render web service (Docker, plan: pro)       │
                            │                                              │
   you / external clients   │  ┌────────────────────────────────────────┐  │
   ─────────HTTPS──────────►│  │  hermes dashboard (port 10000)         │  │
                            │  │  - /api/status (healthcheck)            │  │
                            │  │  - browser UI: config / keys / chat    │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │  ┌────────────────────────────────────────┐  │
   Telegram / Discord /  ◄──┤  │  hermes gateway run (foreground)       │  │
   Slack / etc. (outbound)  │  │  - registers Render MCP @ boot         │  │
   Render MCP @ mcp.render  │  │  - calls mcp_render_* tools            │  │
   ◄──────HTTPS────────────►│  │  - long-polls chat platforms           │  │
                            │  │  - spawns subagents per task           │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │                  ▼                           │
                            │  ┌────────────────────────────────────────┐  │
                            │  │  /opt/data (persistent disk, 10 GB)    │  │
                            │  │  .env, config.yaml, sessions/,         │  │
                            │  │  skills/, memories/, logs/             │  │
                            │  └────────────────────────────────────────┘  │
                            │                                              │
                            │  Image-baked, read-only:                     │
                            │   /opt/render-tools/skills-upstream (skills) │
                            │   /opt/render-tools/skills-local    (overlay)│
                            └──────────────────────────────────────────────┘
```

A single container runs both Hermes processes. The dashboard ([upstream docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-dashboard.md)) is a side-process that the upstream entrypoint backgrounds whenever `HERMES_DASHBOARD=1` is set; the gateway is the foreground PID. They share `/opt/data` and a PID namespace, which is required for the dashboard's gateway-liveness checks.

The disk holds everything that should survive a redeploy: API keys (`.env`), config (`config.yaml`), the FTS5 session database, installed skills, Honcho user models, agent memories, cron job definitions, and logs. The `render-oss/skills` bundle and the bootstrap that registers the Render MCP server are baked into the image (versioned with each deploy), not the disk.

## What's pre-baked for Render

The `Dockerfile` adds two layers on top of `nousresearch/hermes-agent`:

| Layer | Path in container | Source | Pinned via |
|---|---|---|---|
| Render skill bundle | `/opt/render-tools/skills-upstream/` | [render-oss/skills](https://github.com/render-oss/skills) tarball | `RENDER_SKILLS_REF` ARG (commit SHA) |
| Hermes-on-Render overlay | `/opt/render-tools/skills-local/` | [`./skills/`](./skills) in this repo | This repo's commits |

On every boot, the s6-overlay cont-init hook [`scripts/cont-init-patch-config.sh`](scripts/cont-init-patch-config.sh) (installed as `/etc/cont-init.d/016-render-patch-config`) runs an idempotent patcher ([`scripts/patch-config.py`](scripts/patch-config.py)) that adds two entries to `/opt/data/config.yaml` if they're missing:

```yaml
mcp_servers:
  render:
    url: https://mcp.render.com/mcp
    headers:
      Authorization: "Bearer ${RENDER_MCP_API_KEY}"

skills:
  external_dirs:
    - /opt/render-tools/skills-local
    - /opt/render-tools/skills-upstream
```

Those two entries are **insert-only**: the patcher never overwrites edits you make from the dashboard. The `${RENDER_MCP_API_KEY}` placeholder is resolved lazily at gateway startup, so you can rotate the key from Render's **Environment** tab without rebuilding the image — just restart the service.

The overlay skills under `skills/` use names that do not exist upstream (`render-on-hermes`, `kanban-sdlc-worker`, `kanban-sdlc-reviewer`). That is deliberate: two skills with the same name on different `external_dirs` make `skill_view` refuse the ambiguity rather than pick a winner, so an overlay never "shadows" an upstream skill — it composes with it under its own name.

### Kanban SDLC (coder → reviewer → squash-merge)

The same image also turns the Hermes kanban board into a closed-loop SDLC for `howemoney/stopsargassum`. Everything lives in this repo; nothing is hand-configured on the box except the secrets:

| Piece | Path in container | Source | What it does |
|---|---|---|---|
| Config patcher (extended) | `/opt/render-tools/patch-config.py` | [`scripts/patch-config.py`](scripts/patch-config.py) | Adds the `kanban.*` topology, the `auxiliary.*` model picks, and the guard plugin to `/opt/data/config.yaml`; `--profile-config` does the same for the `coder` / `reviewer` profile configs. Two tiers: **insert-only** (yours wins) and **enforced** (`kanban.dispatch_in_gateway`, `orchestrator_profile`, `default_assignee`, `max_in_progress`, `max_in_progress_per_profile`, `auto_decompose`, and `model.default`/`model.provider` on the two profiles — rewritten every boot and logged as `enforced k (old -> new)` so a dashboard edit to one of them is visibly reverted). |
| Boot hook 017 | `/etc/cont-init.d/017-render-kanban-bootstrap` | [`scripts/cont-init-kanban-bootstrap.sh`](scripts/cont-init-kanban-bootstrap.sh) | Creates the `coder` and `reviewer` profiles (+ role `SOUL.md`, descriptions), patches their configs, sets the board's default workdir to `/opt/data/work/stopsargassum`, points that anchor repo at our git hooks, fast-forwards its `main` when clean, and copies the health probe into `/opt/data/scripts/`. Every step is `\|\| true`; it never blocks boot. |
| Guard plugin | `/opt/hermes/plugins/render-kanban-guard/` | [`plugins/render-kanban-guard/`](plugins/render-kanban-guard) | Opt-in via `plugins.enabled`. Fast-forwards the anchor repo right before the dispatcher cuts a worktree; blocks `git push … main`, `--force`, `--no-verify`, and (for workers) `gh pr merge` at the tool layer; injects a short role section into the system prompt of kanban runs so dashboard-created cards still find the right skill. |
| Git pre-push hook | `/opt/render-tools/git-hooks/pre-push` | [`scripts/git-hooks/pre-push`](scripts/git-hooks/pre-push) | Refuses any push to `main`/`master`; inherited by every `.worktrees/*` via `core.hooksPath` on the anchor. **An accident guard, not a security boundary** (`--no-verify` bypasses it; the plugin blocks `--no-verify`, but a determined agent can still script around both). Real protection is GitHub branch protection, which this repo does not have yet. |
| House skills | `/opt/render-tools/skills-local/` | [`skills/kanban-sdlc-worker`](skills/kanban-sdlc-worker), [`skills/kanban-sdlc-reviewer`](skills/kanban-sdlc-reviewer) | The worker protocol (sync, gate, commit, push `wt/<id>`, open PR, `kanban_request_review(reviewer="reviewer")`) and the reviewer protocol (gate, CI wait, `gh pr merge --squash --delete-branch --match-head-commit`, `kanban_complete`). The reviewer skill composes with upstream `sdlc-review`, which the dispatcher force-loads on every review run. |
| Health probe | `/opt/data/scripts/kanban-health.py` | [`scripts/kanban-health.py`](scripts/kanban-health.py) | Zero-LLM `no_agent` cron: `hermes cron create "every 3h" --no-agent --script kanban-health.py --name kanban-health --deliver telegram`. Prints only when something is new (stranded/zombie cards, dispatcher not ticking, stale review, silent worker, red CI/Deploy on main, done digest). `--dry-run` to preview, `--auto-escalate` to pin stuck coder cards to `openai/gpt-5.6-sol`. |

Roles are fixed: `default` orchestrates (decompose/triage, `openai/gpt-5.6-sol`), `coder` implements (`deepseek/deepseek-v4-pro-0813` via OpenRouter, `kanban.default_assignee`), `reviewer` merges (`openai/gpt-5.6-sol`; `max_in_progress_per_profile: 1` serialises merges). Workers need `GH_TOKEN` in the Render **Environment** tab (a fine-grained PAT on `stopsargassum`: contents RW, pull requests RW, checks/actions read). Holds are `hermes pause` / `hermes resume` — nothing else.

> **Why `RENDER_MCP_API_KEY` and not `RENDER_API_KEY`?** The standard name is what the `render` CLI looks for. We deliberately don't ship the CLI in this image (see **Security: agent capabilities**). This is still a normal Render API key with the permissions of the user who created it. The nonstandard env var name avoids accidental CLI auto-auth if you later install the CLI manually. Name your CLI key separately.

## Prerequisites

You need:

- **An LLM provider API key.** [OpenRouter](https://openrouter.ai/keys) is the easiest because it routes to most providers behind a single key. Direct keys for Anthropic, OpenAI, Google, or Hugging Face also work.
- **A Render account** with at least the `standard` plan ($25/month at time of writing). The free plan can't run this image; the `standard` plan has the memory headroom Hermes needs.

Optional, depending on which channels you want Hermes to listen on:

- **A Render API key**, if you want the bundled MCP server to inspect or manage Render resources. Generate one at [`dashboard.render.com/u/*/settings#api-keys`](https://dashboard.render.com/u/*/settings#api-keys) and paste it as `RENDER_MCP_API_KEY`. The agent runs without it, but can't see anything on your Render account.
- **Telegram bot token** from [@BotFather](https://t.me/BotFather), plus your Telegram user ID from [@userinfobot](https://t.me/userinfobot).
- **Discord bot token** from [discord.com/developers/applications](https://discord.com/developers/applications) (enable the Message Content Intent).
- **Slack bot + app-level tokens** from [api.slack.com/apps](https://api.slack.com/apps) (Socket Mode requires both `xoxb-...` and `xapp-...`).

> [!WARNING]
> **A Render API key can expose every workspace linked to your account.**
>
> Hermes can use the key through MCP to inspect any workspace the key's owner can access. Some MCP tools can mutate resources today, and more write-capable tools may be added over time. Use a dedicated low-privilege Render user when possible, and do not paste a personal Owner key unless you accept that risk.

You don't need any optional keys to deploy. You can fill them in via the Render Dashboard after the service is up. `RENDER_MCP_API_KEY` is gated behind `sync: false` in the Blueprint, so the **Deploy to Render** flow will prompt for it.

## Deploy

### Option 1: Deploy button

1. Click the **Deploy to Render** button above.
2. Pick a workspace and a service name.
3. Optionally paste your `RENDER_MCP_API_KEY` when prompted, or leave it blank and add it later from the Environment tab. The agent works without it, just without Render tools.
4. Render reads `render.yaml`, generates a value for `HERMES_GATEWAY_TOKEN`, and creates the service. All other env vars start blank.
5. The first deploy builds the image from the `Dockerfile`. Expect ~3 to 5 minutes for the upstream pull (~2.6 GB compressed) plus our thin Render tooling and skills layers, then ~1 minute for the gateway to boot.

### Option 2: Manual Blueprint sync

1. Fork this repo.
2. In the Render Dashboard, go to **Blueprints** → **New Blueprint Instance** and point at your fork.
3. Confirm and apply.

### Protect the URL before configuring

The Hermes dashboard has no built-in authentication. Anyone who knows the service URL can read and write your API keys. Before you visit the dashboard for the first time, choose how you want to protect it:

- Put the service behind an auth gateway that verifies a bearer token, OAuth session, or trusted identity provider.
- Keep the dashboard reachable only through a private network path, such as Tailscale.
- Accept the risk for a demo, use low-privilege keys, and delete the service when you're done.

Read the **Security** section before you paste production API keys.

## Post-deploy setup

Once the service is healthy (the **Events** tab shows "Deploy live"), open the URL Render assigned (it ends in `.onrender.com`). You'll see the Hermes dashboard.

The Blueprint deliberately keeps the env-var surface tiny. All provider keys, tool keys, and chat platform tokens are set from the dashboard, not from `render.yaml`. The dashboard writes everything to `/opt/data/.env`, which lives on the persistent disk and survives redeploys.

Walk through these tabs in order:

1. **API Keys**. Paste a key for at least one LLM provider. Pick one:
   - `OPENROUTER_API_KEY` from [openrouter.ai/keys](https://openrouter.ai/keys) routes to most providers behind a single key
   - `ANTHROPIC_API_KEY` from [console.anthropic.com](https://console.anthropic.com) for Claude models direct
   - `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `HF_TOKEN`, etc. for the others
2. **Config**. Set the `model` field at the top of the list. The upstream image's default is `anthropic/claude-opus-4.6`, which works as soon as you've set `ANTHROPIC_API_KEY`. Otherwise pick a model your provider supports (for example, `anthropic/claude-sonnet-4.6` for Anthropic, or any OpenRouter model ID like `openai/gpt-5.5`).
3. **Status**. Confirm the gateway is running and the model is reachable. The "Connected platforms" list will be empty until you add a chat platform.
4. **API Keys** again, optionally. If you want a chat gateway, add the matching tokens: `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`, etc. Use the **Restart gateway** button on the Status tab so the new tokens are picked up.

If you'd rather set keys from the Render Dashboard's **Environment** tab (handy for CI or secrets-manager workflows), that path also works: Render env vars override `/opt/data/.env` at process start. Pick one path and stick with it to avoid drift. **The two `RENDER_*` variables are the exception** — set them from the Render **Environment** tab (not the Hermes dashboard's API Keys tab), since `config.yaml` reads `${RENDER_MCP_API_KEY}` from the gateway process environment.

### Verify the Render tools are wired up

From the dashboard's **Chat** tab, ask Hermes to verify the tools:

```
What Render services are running in my account?
```

The agent should call `mcp_render_list_services` and respond with the list. If it instead tells you "I don't have access to Render tools" or similar, the gateway didn't see `RENDER_MCP_API_KEY` at startup — set it under **Environment** and click **Restart gateway** on the Status tab.

Before you ask the agent to mutate Render resources, read the **Security: agent capabilities** section below. The agent can use every Render MCP tool allowed by your API key.

### Where the "gateway token" fits

The Blueprint generates a `HERMES_GATEWAY_TOKEN` for you. Today, upstream Hermes doesn't read this variable directly at runtime: it's a placeholder for the OpenAI-compatible API server's bearer key. If you opt into the API server (set `API_SERVER_ENABLED=true` from the dashboard's **API Keys** tab, then paste this token into `API_SERVER_KEY`), external HTTP clients can authenticate against `/v1/chat/completions` using `Authorization: Bearer <that value>`.

## Chatting with the agent

The simplest way to talk to your deployed Hermes is the dashboard's **Chat** tab. The Blueprint sets `HERMES_DASHBOARD_TUI=1`, which makes the upstream dashboard expose the full TUI in the browser over a server-side PTY plus xterm.js. Slash commands, model picker, tool-call cards, streaming, sessions: everything works the same as a local terminal.

If you'd rather stay on the command line, two paths work, both because the in-container `hermes` is the same binary as the local CLI:

- **One-shot prompts via Render Shell or SSH.** The browser shell on Render does not allocate a TTY for `runtime: image` services. The interactive REPL (`hermes` with no args) will print a banner and quit immediately with `Warning: Input is not a terminal (fd=0)`. Use the non-interactive form instead:

  ```bash
  /opt/hermes/.venv/bin/hermes chat -q "summarize today's logs"
  ```

  This runs one turn, prints the result, and exits cleanly. You can chain it with `--resume <session-id>` to continue an existing conversation.

- **Real terminal via the Render CLI.** From your local machine:

  ```bash
  render ssh <service-id>
  /opt/hermes/.venv/bin/hermes
  ```

  `render ssh` allocates a PTY, so the interactive REPL works.

The chat tab in the dashboard is still the cleanest UX. Use the CLI fallbacks when you're scripting or already in a terminal context.

## Cost expectations

Costs assume Render's published prices in May 2026 and don't include data egress, which is unmetered for typical Hermes traffic.

| Component                     | Plan                              | Cost            |
|-------------------------------|-----------------------------------|-----------------|
| Web service (`runtime: image`) | `pro` (4 GB / 2 CPU) — `render.yaml` since the kanban SDLC (1 coder + 1 reviewer worker on top of gateway + dashboard); `standard` (2 GB, $25/month) is enough without it | $85/month |
| Persistent disk (`/opt/data`)  | 10 GB SSD (anchor repo + `.worktrees/` + npm cache live here) | $2.50/month |
| **Subtotal (this template)**   |                                   | **$87.50/month**|

`pro` is what the kanban concurrency caps (`max_in_progress: 2`, per-profile 1) were sized for; measure memory on Render before raising them. The starter plan (512 MB) cannot hold the Hermes image and is not supported.

LLM costs are separate and depend entirely on your provider and usage. OpenRouter and Anthropic both report usage in their respective dashboards; Hermes also surfaces per-model usage on its **Analytics** page.

## Updating

Both pinned versions live in the [`Dockerfile`](Dockerfile) as build args:

```dockerfile
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.5.7
ARG RENDER_SKILLS_REF=1b8496570748203351f628b2ae738805ac2c23d5
```

Bump either, commit, and push. Render won't auto-deploy (the Blueprint sets `autoDeployTrigger: off`); trigger a manual deploy from the Dashboard or the [Render CLI](https://render.com/docs/cli) on your own machine:

```bash
render deploys create <service-id>
```

Your `/opt/data` disk is untouched across image upgrades. The upstream entrypoint runs a manifest-based `skills_sync.py` on each boot, which preserves edits to bundled Hermes skills. The `render-oss/skills` bundle and the `render-on-hermes` overlay live under `/opt/render-tools/` (read-only image layer), so they're replaced wholesale on every new build and never touch the disk.

Hermes ships fast: roughly weekly tagged releases, each with around 180 commits. Check [the upstream releases page](https://github.com/NousResearch/hermes-agent/releases) before bumping `HERMES_IMAGE`. The [skills repo's commit log](https://github.com/render-oss/skills/commits/main) is the source of truth for `RENDER_SKILLS_REF`.

## Troubleshooting

### Logs

Render keeps logs in the **Logs** tab of your service. Filter by stream:

- The dashboard side-process prefixes its lines with `[dashboard]`.
- Gateway and agent logs are unprefixed.
- For deeper inspection, log files also live on disk at `/opt/data/logs/` (`agent.log`, `errors.log`, `gateway.log`).

You can tail them from the dashboard's **Logs** tab too, or via SSH (next section).

### Shell access

Render gives you SSH into the container. From the service's overview page, click **Shell** (browser PTY) or copy the SSH command from **Settings**.

```bash
# Inspect the data volume.
ls /opt/data
cat /opt/data/.env

# Run the Hermes CLI directly.
/opt/hermes/.venv/bin/hermes status
/opt/hermes/.venv/bin/hermes config get model.default
```

The container runs as the `hermes` user (UID 10000), not root.

### Service won't start

Check the **Events** tab for the deploy that failed, then the **Logs** tab around that timestamp.

| Symptom                                              | Likely cause                                                                 |
|------------------------------------------------------|------------------------------------------------------------------------------|
| `Refusing to start: binding to 0.0.0.0 requires API_SERVER_KEY` | You set `API_SERVER_ENABLED=true` and `API_SERVER_HOST=0.0.0.0` without an `API_SERVER_KEY`. Set the key or flip back to `127.0.0.1`. |
| Health check fails on `/api/status`                  | `HERMES_DASHBOARD` is unset or the dashboard crashed. Check `[dashboard]` lines for a Python traceback. |
| Container OOM-killed                                 | Bump plan to `pro`. Playwright/Chromium is the usual culprit.                 |
| `Permission denied` on `/opt/data/...`               | The disk was attached after a deploy that ran as a different UID. Restart the service; the entrypoint chowns `/opt/data` on boot when run as root. |
| `Warning: Input is not a terminal (fd=0)` then `Goodbye!` when running `hermes` | Render's browser shell pipes stdin instead of allocating a PTY. Chat from the dashboard's **Chat** tab, or use `hermes chat -q "..."`, or `render ssh <service-id>` from a local terminal. |
| `Goodbye! ⚕` in the deploy logs followed by 502s on the URL | The image's own `ENTRYPOINT` got bypassed (an `ENTRYPOINT` override in a fork, or a `dockerCommand` in `render.yaml`). This Dockerfile deliberately sets **no** `ENTRYPOINT` — the base image's `entrypoint-dispatch.sh` must run, and only `CMD ["gateway", "run"]` is ours. Do not reintroduce a `tini`/wrapper entrypoint: as of base image v2026.8.3 `/usr/bin/tini` is a shim and `docker/entrypoint.sh` is a deprecated shim that never execs the CMD. |
| `Refusing to run the Hermes gateway as root` | Same root cause as above. Drop any `ENTRYPOINT` override so the upstream dispatcher can do its `s6-setuidgid` drop. |
| `Refusing to bind dashboard to 0.0.0.0 — ... no auth providers are registered` followed by `update_failed` | Hermes v0.20.0 made the dashboard auth gate fail-closed on non-loopback binds, and `--insecure` is now accepted-and-ignored. Set `HERMES_DASHBOARD_OIDC_ISSUER` / `_CLIENT_ID` / `_CLIENT_SECRET` (from the Cloudflare Access SaaS app — see below) plus `HERMES_DASHBOARD_PUBLIC_URL`. Without a registered auth provider the dashboard never binds port 10000, so the health check can never pass and every deploy fails. |
| OIDC login loops, or `issuer mismatch` in the dashboard logs | `HERMES_DASHBOARD_OIDC_ISSUER` must be the **per-application** Cloudflare URL, `https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<client_id>` — not the team root. The team-level `/.well-known/openid-configuration` returns only `issuer` and `jwks_uri`, with no `authorization_endpoint` or `token_endpoint`, so discovery fails. Also confirm the Access app's Redirect URL is exactly `https://hermes.howe.ceo/auth/callback`. |
| Dashboard **Chat** tab shows "Chat unavailable: 1" or hangs / 500s on `/api/pty` | Two upstream bugs combined to break the Chat tab on hosted deploys: (1) [#20500](https://github.com/NousResearch/hermes-agent/issues/20500): `/opt/hermes/ui-tui/` ships root-owned but the dashboard runs as the `hermes` user, so the runtime esbuild rebuild fails with `EACCES`. (2) Separate filename mismatch: `_hermes_ink_bundle_stale()` in `hermes_cli/main.py` looks for `packages/hermes-ink/dist/ink-bundle.js`, but `@hermes/ink`'s build script (`esbuild src/entry-exports.ts --outdir=dist`) only produces `entry-exports.js`. The bundle the staleness check expects is never created, so every `/api/pty` connect runs a 28-second `npm run build` that exceeds Render's WebSocket-upgrade timeout. The Dockerfile chowns the directories AND `touch`es the two expected paths at build time so both checks short-circuit. If you've forked the template and removed those lines, restore them. |
| Red `Unauthorized` on the Chat tab's attach/paste, while everything else works | A dashboard plugin on the data disk is doing its own auth with the legacy loopback token (`X-Hermes-Session-Token` / `web_server._SESSION_TOKEN`). In gated OIDC mode that token is never injected into the SPA, so the plugin 401s every browser request — even fully authenticated ones. The retired `dropzone` plugin (see `archive/dropzone-plugin/`) was exactly this; its final bundle also hid the working native paperclip. Plugin API routes must rely on the gate's cookie auth (any request that reaches `/api/plugins/<name>/` has already cleared `gated_auth_middleware`) — never re-check the loopback token. |
| Dashboard silently "logs you out": cookie-gated `/api/*` calls start returning 401 `{"reason":"no_cookie"}` while the Chat terminal keeps streaming | The session cookie's Max-Age equals the ID token's `exp`, and without `offline_access` no refresh token is ever issued — so the session dies at ID-token expiry (minutes, with Cloudflare Access defaults). The chat PTY websocket authenticates once at connect and never re-auths, which is why it survives. Set `HERMES_DASHBOARD_OIDC_SCOPES="openid profile email offline_access"` (in `render.yaml` since 2026-08-18), enable refresh tokens on the Access SaaS app, and consider raising its session duration. |
| `mcp_render_*` tools missing from Hermes' tool list | The gateway started without `RENDER_MCP_API_KEY`. Add it under the service's **Environment** tab and click **Restart gateway** from the dashboard's Status tab. |
| Agent says it tried to run `render <something>` and got `command not found` | Working as designed — the Render CLI is not installed in this image (see **Security: agent capabilities**). Most CLI capabilities have an MCP equivalent the agent should use instead; the rest (live log streaming, `render psql`, SSH) the user runs from their own machine. |
| `[render-tools] config patch failed; continuing` in the boot logs | Non-fatal. The agent still runs; you just won't see the Render MCP server until you fix it. Usually means `/opt/data/config.yaml` isn't valid YAML — fix it from the dashboard or wipe it (see "Forcing a clean rebuild"). |
| `tirith security scanner enabled but not available`  | Harmless. Tirith is an optional Rust-based command scanner; without it, Hermes uses pattern matching. Ignore unless you specifically want native scanning. |
| `[render-tools] kanban-bootstrap: warning: …` in the boot logs | Non-fatal by design — every 017 step is `\|\| true`. Common ones: `profile create 'coder' failed or timed out` (re-runs repair it next boot; `hermes -p coder update` re-seeds bundled skills), `anchor … is dirty or mid-merge/rebase; NOT syncing main` (someone left uncommitted work in `/opt/data/work/stopsargassum` — clean it by hand, the hook never resets a dirty tree), `anchor fetch failed or timed out` (no `GH_TOKEN`, or the anchor remote is SSH without a key — `git -C /opt/data/work/stopsargassum remote get-url origin`). |
| Cards sit in `ready` forever; `hermes kanban diagnostics` says `stranded_in_ready`; health cron says "is the in-gateway dispatcher ticking?" | No live dispatcher. Check the gateway log for `kanban dispatcher: holding singleton dispatcher lock` and `embedded in gateway`. If absent: `hermes config get kanban.dispatch_in_gateway` must be `true` (enforced every boot by the patcher — if it is `false`, someone set `HERMES_KANBAN_DISPATCH_IN_GATEWAY` in the Render env; remove it, it is disable-only), then **Restart gateway**. `hermes kanban dispatch --dry-run --json` shows what would be picked up. |
| Root gateway log says `kanban dispatcher: another gateway already holds the dispatcher lock (/opt/data/kanban/.dispatcher.lock); this gateway will NOT dispatch` | A **profile gateway** (e.g. `engine-research`, auto-started by upstream `02-reconcile-profiles` because its last state was `running`) won the shared singleton lock ahead of the root gateway. That gateway then dispatches with *its own* `config.yaml`, not the root config the patcher enforces. The 017 hook pins `kanban.dispatch_in_gateway: false` into **every** `/opt/data/profiles/*/config.yaml` (`patch-config.py --profile other` for profiles we don't own) so only the root gateway is a candidate; a profile that was already up keeps the lock until its next restart — restart it (or redeploy). Seen 2026-08-19. |
| Two dispatchers: cards get claimed twice, or `kanban daemon` in `pgrep -af kanban` | A standalone `hermes kanban daemon` is running next to the in-gateway one (pre-cutover leftover, or a cron/skill restarts it). `kill` it, `pgrep -af "kanban daemon"` must be empty, and grep `hermes cron list` + skills for `kanban daemon`. The singleton flock (`/opt/data/kanban/.dispatcher.lock`) makes the loser log `another dispatcher holds the lock`, so this is usually noisy rather than corrupting — but only one of them carries our `render-kanban-guard` hooks. |
| Zombie cards: `running` with no heartbeat for > 90 min, or `protocol violation … exited without kanban_complete/kanban_block` | The worker process died or the model printed a summary and stopped. Upstream's `dispatch_stale_timeout_seconds` (4 h, insert-only) and `reconcile_orphans` reap them into `blocked`/retry; the health cron flags them earlier. `hermes kanban show <id>` + `hermes kanban log <id>` for the last run; `hermes kanban set-model <id> openai/gpt-5.6-sol --provider openrouter` to pin a stronger model (`kanban-health.py --auto-escalate` does exactly this). Never `kanban complete` a card by hand unless the work really landed on `origin/main`. |
| Cards blocked with "HOLD ENFORCED" / "HOLD" text in the reason | A pre-cutover watcher wrote them; there is no HOLD mechanism any more. The only hold is `hermes pause --reason "…"` / `hermes resume` (the `ESTOP` sentinel, which the health cron reports when older than 6 h). `hermes kanban unblock <id>` or `hermes kanban schedule <id> …` — never delete. |
| A human pushed straight to `main` / bot branch protection | There is **no GitHub branch protection** on `stopsargassum`. The pre-push hook + plugin only stop the agents' *accidental* pushes; anyone with the token can still push `main`. Audit: `git log --since=<cutover> --format='%ce %s' origin/main \| grep -v noreply@github.com` should be empty (squash-merges are authored by GitHub). Enabling branch protection + "require PR" on GitHub is the real fix and is a follow-up. |
| Reviewer merged but the remote `wt/<id>` branch is still there, or the worktree under `.worktrees/` never disappears | `gh pr merge --delete-branch` run *inside* the linked worktree without `-R <repo>` tries `git checkout main` (fails: `main` is checked out in the anchor) and exits before the remote delete. The reviewer skill always passes `-R howemoney/stopsargassum`. A leftover worktree whose branch is merged: `git -C /opt/data/work/stopsargassum worktree remove .worktrees/<id>` then `git worktree prune`. Never `git fetch --prune` between a branch delete and the card's `kanban_complete` — upstream's cleanup reads `refs/remotes/origin/wt/<id>` to decide whether commits are pushed. |
| Rolling back the SDLC layer | Three independent levers, none touch the disk: (1) Render **Rollback** to the previous deploy (image swap; `/opt/data` untouched, but the enforced config keys stay whatever the last boot wrote — set `kanban.dispatch_in_gateway: false` from the dashboard if you also want the dispatcher off); (2) `hermes pause --reason "…"` stops all dispatch immediately without any redeploy; (3) add `render-kanban-guard` to `plugins.disabled` (root and `-p coder` / `-p reviewer`) — upstream skips a plugin named there even when it is also in `plugins.enabled` (`hermes_cli/plugins.py:3893-3911`), and the patcher only ever appends to `enabled`, so this survives reboots; removing it from `enabled` does not (the next boot re-appends it). |

### Changing env vars

Set, change, or delete env vars under the service's **Environment** tab. Render restarts the container after a save. Hermes also exposes a `/reload` slash command for in-session reloads if you've already started chatting from the CLI; it's not relevant for the gateway, which restarts cleanly.

### Forcing a clean rebuild

If the Hermes data directory gets into a bad state (corrupt session DB, partial skill install), wipe it:

1. SSH in.
2. `mv /opt/data /opt/data.bak && exit`.
3. Restart the service from the Render Dashboard. The entrypoint recreates the directory tree and reseeds defaults.

Or restore the most recent automatic disk snapshot from the **Disks** page.

## Security

There are two distinct security surfaces in this template, and they compound:

1. **Dashboard auth.** Hermes' web dashboard has no authentication. Anyone who reaches the URL can read your provider keys, change configuration, and chat with the agent.
2. **Agent capabilities.** The agent has access to a Render workspace API key via MCP. Depending on that key's role, it can restart services, change env vars, trigger deploys, and run SQL against Render Postgres.

The two compose into a worst case: an unauthenticated user reaches the dashboard, chats with the agent, and asks it to "delete all services in this workspace." This template registers the full Render MCP tool catalog and **does not install the `render` CLI**. The dashboard lock is on you.

### Agent capabilities

The agent can reach Render through MCP. The boot-time patcher registers `mcp_servers.render` without a `tools.include` filter, so Hermes sees every tool exposed by the Render MCP server. The effective permission boundary is the Render role behind `RENDER_MCP_API_KEY`, across every workspace that key can access.

This is intentionally permissive. It avoids tool visibility surprises, but it means the agent can call write-capable tools when the API key allows them. Even if most MCP usage is read-oriented today, treat the dashboard URL and API key like an admin surface.

#### Why we don't ship the Render CLI

The [`render` CLI](https://render.com/docs/cli) is useful for local operator workflows, but this image does not install it. MCP is the supported in-container Render integration. If you need the CLI, install it deliberately and inspect any installer before running it.

The variable bound in the gateway environment is named `RENDER_MCP_API_KEY` rather than the stock `RENDER_API_KEY` so a manually installed CLI does not auto-authenticate from this var. This does not create a different kind of API key. The Render account role behind the key limits agent capabilities.

This trade-off is worth revisiting once Render adds scoped API keys. A read-only-scoped key for routine inspection and a write-scoped key for deliberate actions would be a better posture.

#### Concrete steps to harden further

- **Scope the API key with a workspace member role.** Create a separate Render workspace member with the minimum role you need and use that user's API key for `RENDER_MCP_API_KEY` instead of an Owner key. The agent inherits whatever role the key grants. This is the closest thing to scoped keys available today.
- **Lock the dashboard.** Put authentication or private-network access in front of the service. Without that, anyone reaching the URL can ask the agent to do anything within whatever caps you've set above.

The bundled `render-on-hermes` overlay skill tells the agent that MCP is already configured and that CLI installation is not an automatic fallback. But **do not rely on agent-side guardrails for safety**. An LLM cannot meaningfully self-restrict. Dashboard access control and a least-privileged API key are the real defenses.

### Dashboard access

Even if the Render API key cannot mutate resources, the dashboard still leaks your LLM provider keys to whoever reaches it. Anyone who can chat with the agent can ask it to do anything the API key allows. Lock the dashboard down before pasting any keys.

Two practical options.

#### Option A: Auth gateway

Expose a small authenticated Web Service in front of Hermes and keep Hermes itself private. The gateway verifies a bearer token, OAuth session, or identity-provider token, then forwards approved traffic to Hermes over Render's private network.

This is the most portable option because it does not depend on static client IPs.

#### Option B: Tailscale

Skip the public internet entirely. Run Tailscale on a sidecar (or use Render's [Tailscale template](https://render.com/docs/deploy-tailscale-derp)) and reach the dashboard only from devices on your Tailnet. This takes more setup, but it avoids IP rotation pain and works from anywhere.

#### Notes

- These options compose. For example, an auth gateway can still sit behind a private network path.
- The OpenAI-compatible API server (`API_SERVER_ENABLED=true`) is separate from the dashboard. It uses a bearer token (`API_SERVER_KEY`), so it's safe to expose with a long random key, but this Blueprint doesn't route it publicly.
- For broader Hermes security guidance see the [upstream security doc](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md).

## What this template does and doesn't do

What it does:

- Pins a specific upstream Hermes image and `render-oss/skills` commit for reproducible deploys.
- Runs the Hermes gateway and dashboard inside one container, the way upstream supports.
- Mounts a persistent disk at the upstream-default `HERMES_HOME` path.
- Bakes the official Render skill bundle into the image, plus a small `render-on-hermes` overlay skill that tells the agent how to behave on this host.
- Idempotently patches `config.yaml` on each boot to register the Render MCP server with the full MCP tool catalog available to your API key, without overwriting your edits.
- Generates a `HERMES_GATEWAY_TOKEN` and marks `RENDER_MCP_API_KEY` as `sync: false` so secrets never sync from the repo.
- Sets a healthcheck that probes the dashboard.

What it deliberately doesn't do:

- **It doesn't install the `render` CLI.** MCP is the supported in-container Render integration. Install the CLI only as a deliberate operator choice.
- It doesn't try to add authentication on top of the dashboard. Use an auth gateway, private network path, or another access-control layer you trust.
- It doesn't enable the OpenAI-compatible API server. Flip `API_SERVER_ENABLED=true` and supply `API_SERVER_KEY` if you need it.
- It doesn't ship a default model. Hermes' upstream default is set in `config.yaml`, which lives on disk and is owner-configurable from the dashboard.
- It doesn't configure browser automation tweaks (`--shm-size`, GPU access). Those need an instance type with more RAM, not extra Render config.
- It doesn't fork or modify the upstream `render-oss/skills` content. The overlay in `skills/render-on-hermes/` is the only Hermes-specific addition; everything else is the canonical Render skill bundle.

## License

This template is MIT licensed (see [`LICENSE`](./LICENSE)). Hermes Agent itself is also MIT licensed; see [the upstream LICENSE](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE).
