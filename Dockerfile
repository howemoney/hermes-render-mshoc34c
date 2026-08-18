# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render, pre-baked with Render tooling.
#
# Extends the upstream NousResearch/hermes-agent image with:
#   - A bundle of Render-focused skills mounted via skills.external_dirs
#   - A boot-time patcher that registers the Render MCP server in
#     config.yaml (idempotent; never overwrites user edits)
#
# We deliberately do NOT install the `render` CLI. This image is configured
# around the Render MCP server; installing extra CLIs should be a conscious
# operator choice, not something the agent does as an automatic fallback.
#
# Pin the upstream tag here. Bump and redeploy to upgrade Hermes.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.8.3
FROM ${HERMES_IMAGE}

# Workarounds for upstream issues that prevent the dashboard's Chat tab
# from connecting on hosted deploys. Baked into the image so the runtime
# command stays simple. See render.yaml comments + the README for context.
#   - chown: dashboard runs as `hermes` but ui-tui/ + node_modules/ ship root-owned
#   - touch ink-bundle.js: short-circuits _hermes_ink_bundle_stale()
#   - touch entry.js: bumps mtime above source .ts files so _tui_build_needed() returns False
USER root
RUN chown -R hermes:hermes /opt/hermes/ui-tui /opt/hermes/node_modules \
 && mkdir -p /opt/hermes/ui-tui/packages/hermes-ink/dist /opt/hermes/ui-tui/dist \
 && touch /opt/hermes/ui-tui/packages/hermes-ink/dist/ink-bundle.js \
          /opt/hermes/ui-tui/dist/entry.js \
 && chown -R hermes:hermes /opt/hermes/ui-tui

# Bake the Langfuse SDK into the image. The observability/langfuse plugin ships
# in the base image but the `langfuse` Python package does not, so traces never
# emit. It cannot be added at runtime: the gateway runs as the unprivileged
# `hermes` user while /opt/hermes/.venv/lib/.../site-packages is root-owned, so
# a runtime install fails with EACCES. Installing here (as root, at build time)
# persists it across redeploys and lets the plugin send traces once the
# HERMES_LANGFUSE_* keys are set.
#
# The venv is uv-managed (see /opt/hermes/.venv/pyvenv.cfg) and ships without
# pip / ensurepip, so `pip install` is not available. Use the `uv` binary that
# the base image already provides, targeting the venv's interpreter explicitly.
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache langfuse

# Bake the FAL client into the image so image generation works.
#
# The `image_generate` tool routes through FAL.ai (`fal-ai/flux-2/klein/9b` by
# default; the catalog also includes GPT Image, Gemini/Nano-Banana, Ideogram,
# Recraft, Qwen, Krea). The `fal-client` package lives in upstream's `fal`
# extra, which is deliberately excluded from `[all]` (image backends are meant
# to lazy-install at first use via tools/lazy_deps.py). On this host that
# lazy-install path fails: it shells to `pip`, but the uv-managed venv ships
# no pip/ensurepip, and the runtime user (uid 10000) cannot write the
# root-owned site-packages anyway (EACCES). Same reason as the langfuse bake
# above — install here as root, at build time, so it persists across redeploys
# and the image toolset reports available=True.
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache 'fal-client==0.13.1'

# Put `hermes` on PATH for spawned processes. The kanban dispatcher shells out
# to a bare `hermes` to start each worker, but the binary lives in the venv at
# /opt/hermes/.venv/bin/hermes, which is not on PATH for dispatcher-spawned
# children — so every assigned task is silently SKIPPED and no worker ever runs
# (observed 2026-08-09: 7 ready+assigned tasks, zero dispatches).
#
# This cannot be fixed at runtime: the agent runs as uid 10000 (`hermes`) and
# /usr/local/bin is root-owned, so the symlink fails with EACCES and `sudo` is
# not installed. Creating it here, as root at build time, persists across
# redeploys.
RUN ln -sf /opt/hermes/.venv/bin/hermes /usr/local/bin/hermes \
 && /usr/local/bin/hermes --version >/dev/null

# Bake operator CLIs into the image. The header note above says installing
# extra CLIs should be a conscious operator choice — this is that choice: the
# Render + Cloudflare MCP servers cover most flows, but the human operator
# asked for the matching CLIs for shell-level use.
#
# Render CLI: fetched from GitHub releases, arch-matched to the build platform
# via TARGETARCH (amd64 on Render, arm64 on local Apple-Silicon test builds).
# The release zip carries a single `cli` binary; extract with python's stdlib
# zipfile (no `unzip` dependency) and install it as `render`. Authenticates
# non-interactively at runtime via RENDER_API_KEY.
ARG RENDER_CLI_VERSION=2.22.0
ARG TARGETARCH
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    url="https://github.com/render-oss/cli/releases/download/v${RENDER_CLI_VERSION}/cli_${RENDER_CLI_VERSION}_linux_${arch}.zip"; \
    curl -fsSL --retry 3 -o /tmp/render-cli.zip "$url"; \
    python3 -m zipfile -e /tmp/render-cli.zip /tmp/render-cli/; \
    bin="$(find /tmp/render-cli -type f | grep -viE '\.(txt|md|zip)$|license|readme|changelog' | head -n1)"; \
    test -n "$bin"; \
    install -m 0755 "$bin" /usr/local/bin/render; \
    rm -rf /tmp/render-cli /tmp/render-cli.zip; \
    render --version

# Cloudflare Wrangler: pure-Node CLI. Pinned to the v3 line on purpose — the
# base image ships Node 20, but Wrangler v4.119+ hard-requires Node 22 and
# refuses to run otherwise. Wrangler v3 supports Node 18+ and covers every
# deploy/list/KV/R2/D1 flow. Bump this once the base image moves to Node 22.
# Installed globally as root at build time. Authenticates at runtime via
# CLOUDFLARE_API_TOKEN — the same token as the Cloudflare MCP server.
ARG WRANGLER_VERSION=3
RUN npm install -g "wrangler@${WRANGLER_VERSION}" \
 && wrangler --version

# GitHub MCP server (official Go binary), run over stdio. We bake the binary
# instead of using GitHub's hosted https://api.githubcopilot.com/mcp/ endpoint,
# which returned 400 to a plain PAT. The local server talks straight to the
# GitHub API with a token passed via GITHUB_PERSONAL_ACCESS_TOKEN, wired in
# config.yaml as an mcp_servers stdio entry. Arch names differ from Docker's:
# goreleaser ships x86_64 (amd64) and arm64.
ARG GITHUB_MCP_VERSION=1.8.0
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) gharch=x86_64 ;; \
      arm64) gharch=arm64 ;; \
      *) gharch="${TARGETARCH}" ;; \
    esac; \
    url="https://github.com/github/github-mcp-server/releases/download/v${GITHUB_MCP_VERSION}/github-mcp-server_Linux_${gharch}.tar.gz"; \
    curl -fsSL --retry 3 -o /tmp/ghmcp.tar.gz "$url"; \
    tar -xzf /tmp/ghmcp.tar.gz -C /tmp; \
    bin="$(find /tmp -maxdepth 2 -type f -name github-mcp-server | head -n1)"; \
    test -n "$bin"; \
    install -m 0755 "$bin" /usr/local/bin/github-mcp-server; \
    rm -rf /tmp/ghmcp.tar.gz "$bin"; \
    github-mcp-server --help >/dev/null

# Pull the official Render skill bundle from github.com/render-oss/skills
# at a pinned commit. Mounted via skills.external_dirs at boot, so the
# upstream Hermes skills-sync flow never touches these files. To upgrade,
# bump RENDER_SKILLS_REF (a commit SHA, tag, or branch) and rebuild.
ARG RENDER_SKILLS_REPO=render-oss/skills
ARG RENDER_SKILLS_REF=1b8496570748203351f628b2ae738805ac2c23d5
RUN set -eu; \
    tmp="$(mktemp -d)"; \
    url="https://codeload.github.com/${RENDER_SKILLS_REPO}/tar.gz/${RENDER_SKILLS_REF}"; \
    curl -fsSL --retry 3 -o "${tmp}/skills.tar.gz" "${url}"; \
    tar -xzf "${tmp}/skills.tar.gz" -C "${tmp}"; \
    extracted="$(find "${tmp}" -maxdepth 2 -type d -name 'skills' | head -n 1)"; \
    test -n "${extracted}" || { echo "could not find skills/ in tarball" >&2; exit 1; }; \
    install -d -o hermes -g hermes -m 0755 /opt/render-tools/skills-upstream; \
    cp -a "${extracted}/." /opt/render-tools/skills-upstream/; \
    chown -R hermes:hermes /opt/render-tools/skills-upstream; \
    rm -rf "${tmp}"; \
    echo "${RENDER_SKILLS_REPO}@${RENDER_SKILLS_REF}" > /opt/render-tools/skills-upstream/.source

# Local overlay: a Hermes-specific `render-on-hermes` skill that tells
# the agent the MCP server is pre-wired (so skip "install MCP" from
# upstream skills) and that the CLI is deliberately absent (so don't
# try to invoke it). Listed FIRST in skills.external_dirs so same-named
# overlays would shadow upstream entries.
COPY --chown=hermes:hermes skills/ /opt/render-tools/skills-local/

# Repair the dashboard Files page. Upstream's GET /api/files builds its entry
# list in a comprehension, so ONE child of the locked root (/opt/data on a
# hosted deploy) that resolves outside it — any symlink off the data disk, and
# the agent makes those — raises 403 and the page shows nothing but
# `Error: 403: {"detail":"Path outside managed files root"}`. The patch skips
# and logs such entries instead of failing the request; the sandbox is
# unchanged. Applied at build time because /opt/hermes is root-owned and the
# gateway runs as uid 10000. Fails the build if upstream moves the code — see
# the script's docstring, and delete both once upstream fixes the listing.
COPY --chown=root:root scripts/patch-files-page.py /opt/render-tools/patch-files-page.py
RUN python3 /opt/render-tools/patch-files-page.py /opt/hermes/hermes_cli/web_server.py \
 && /opt/hermes/.venv/bin/python -c "import ast,pathlib; ast.parse(pathlib.Path('/opt/hermes/hermes_cli/web_server.py').read_text())"

# Fix gated-mode upload authentication and add unified Chat attachments.
# The patch adds a general document-cache endpoint, a paperclip/file picker,
# and image/document drag-drop. It also makes raw upload fetches follow the
# structured OIDC 401 login_url instead of painting "Unauthorized" in red.
# Rebuild the Vite bundle after patching the TypeScript source, then copy the
# generated assets into the package directory served by web_server.py.
COPY --chown=root:root scripts/patch-chat-attachments.py /opt/render-tools/patch-chat-attachments.py
RUN python3 /opt/render-tools/patch-chat-attachments.py /opt/hermes \
 && /opt/hermes/.venv/bin/python -c "import ast,pathlib; ast.parse(pathlib.Path('/opt/hermes/hermes_cli/web_server.py').read_text()); ast.parse(pathlib.Path('/opt/hermes/hermes_cli/web_models.py').read_text())" \
 && cd /opt/hermes \
 && npm run build --workspace web \
 && rm -rf /opt/hermes/hermes_cli/web_dist \
 && cp -a /opt/hermes/web/dist /opt/hermes/hermes_cli/web_dist

# Boot-time config patch, wired in as an s6-overlay cont-init hook.
#
# Numbered 016- so it lands after the upstream 01-hermes-setup hook (which
# seeds and chowns config.yaml) and before 02-reconcile-profiles starts a
# gateway. Every cont-init.d hook finishes before s6-rc starts main-hermes
# and dashboard, so the patch is applied before anything reads the config.
COPY --chown=root:root scripts/patch-config.py /opt/render-tools/patch-config.py
COPY --chmod=0755 scripts/cont-init-patch-config.sh /etc/cont-init.d/016-render-patch-config
RUN chmod 0755 /opt/render-tools/patch-config.py

# Pre-create the dir the patcher writes to so chown works cleanly on
# first boot. The mounted disk replaces this empty dir at runtime;
# baking it just keeps the image self-contained for any non-disk use.
RUN install -d -o hermes -g hermes -m 0755 /opt/data

# No ENTRYPOINT override — inherit the base image's entrypoint-dispatch.sh.
#
# Through v2026.5.7 this image set `ENTRYPOINT ["/usr/bin/tini", "-g", "--",
# "/opt/render-tools/bootstrap.sh"]`. That silently became fatal in v2026.8.3:
# /usr/bin/tini is now a shim that strips the tini flags and execs
# `/init main-wrapper.sh <our args>`, so s6 came up and then ran bootstrap.sh
# as the container's main program. bootstrap.sh ended with
# `exec /opt/hermes/docker/entrypoint.sh`, which in v2026.8.3 is a deprecated
# shim that re-runs the stage2 bootstrap and deliberately does NOT exec the
# CMD. The main program therefore exited immediately, s6 tore down the whole
# supervision tree, and the deploy failed its health check. Upstream's
# migration note says exactly this: "drop the override — docker will use the
# image's default ENTRYPOINT dispatcher, which handles bootstrap AND CMD."
#
# CMD stays: main-wrapper.sh routes a non-executable first arg through
# `hermes <args>`, so this still resolves to `hermes gateway run`.
CMD ["gateway", "run"]