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
#   v2026.8.3  (v0.20.0) -> v2026.8.18 (v0.20.4) on 2026-08-19: brings the
#   kanban fixes this deployment depends on (reconcile_orphans for zombie
#   claims, a real review lifecycle + the bundled sdlc-review skill,
#   worktree cleanup, `hermes pause`). Boot contract (entrypoint-dispatch.sh,
#   s6 services, cont-init hooks) is unchanged between the two tags.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.8.31
FROM ${HERMES_IMAGE}

# Everything below runs as root at build time; the runtime user is still the
# upstream `hermes` (uid 10000) via entrypoint-dispatch.sh.
#
# (Up to v2026.8.3 this block also chown'ed /opt/hermes/ui-tui + node_modules
# and touched two bundle files so the dashboard Chat tab would not try an
# in-place TUI rebuild as the unprivileged user. Upstream now bakes a prebuilt
# TUI bundle and sets HERMES_TUI_DIR=/opt/hermes/ui-tui, so the launcher takes
# the read-only fast path and that workaround is obsolete.)
USER root

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

# NVIDIA SkillSpector: pinned security scanner + MCP server. Install into a
# dedicated environment so its dependency graph cannot perturb Hermes. The
# commit pin is the exact source revision audited by Howe Agency.
ARG SKILLSPECTOR_REF=7805bb94843d91cb9937f57264ca52642164499b
RUN uv venv --python 3.13 /opt/skillspector \
 && uv pip install --python /opt/skillspector/bin/python --no-cache \
      "skillspector[mcp] @ git+https://github.com/NVIDIA/SkillSpector.git@${SKILLSPECTOR_REF}" \
 && /opt/skillspector/bin/skillspector --version

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
#
# As of v2026.8.18 this is belt-and-braces rather than load-bearing: upstream
# now exports PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:..." image-wide
# (hermes-v818/Dockerfile:420) with a privilege-drop shim at
# /opt/hermes/bin/hermes, and the dispatcher's `_resolve_hermes_argv`
# (hermes_cli/kanban_db.py:10578-10616) falls back to
# `sys.executable -m hermes_cli.main` when no `hermes` is on PATH at all. The
# symlink stays so `docker exec` / cron / any PATH-stripped child still
# resolves a bare `hermes` the same way it did on v2026.8.3.
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

# GitHub CLI. Kanban workers push a `wt/<task>` branch and open a PR; the
# reviewer waits on CI (`gh pr checks --watch`) and squash-merges
# (`gh pr merge --squash`). The upstream image ships `git` but not `gh`, and
# the dispatcher-spawned workers have no MCP servers, so the CLI is the
# integration. Authenticates non-interactively via GH_TOKEN at runtime
# (fine-grained PAT scoped to the target repo; set in the Render Environment
# tab). Static tarball, arch-matched via TARGETARCH (amd64 / arm64).
ARG GH_CLI_VERSION=2.97.0
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    url="https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSL --retry 3 -o /tmp/gh.tar.gz "$url"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_CLI_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_CLI_VERSION}_linux_${arch}"; \
    gh --version

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

# Require a completed NVIDIA SkillSpector score before `hermes skills publish`
# can create an external PR. Hermes' native scan remains as a second gate.
COPY --chown=root:root scripts/patch-skillspector-publish-gate.py /opt/render-tools/patch-skillspector-publish-gate.py
RUN python3 /opt/render-tools/patch-skillspector-publish-gate.py /opt/hermes/hermes_cli/skills_hub.py \
 && /opt/hermes/.venv/bin/python -c "import ast,pathlib; ast.parse(pathlib.Path('/opt/hermes/hermes_cli/skills_hub.py').read_text())"
# Vision never-fail-silent: upstream Hermes v2026.8.18 (v0.20.4) already
# eliminated this problem by replacing _enrich_with_attached_images with
# _build_image_ref_message (tui_gateway/server.py:7173), which no longer
# pre-analyzes images at all — the agent examines them via vision_analyze
# in-loop. No patch needed.

# Fix gated-mode upload authentication and add unified Chat attachments.
# The patch adds a general document-cache endpoint, a paperclip/file picker,
# and image/document drag-drop. It also makes raw upload fetches follow the
# structured OIDC 401 login_url instead of painting "Unauthorized" in red.
# Rebuild the Vite bundle after patching the TypeScript source. Upstream Vite
# already emits directly into hermes_cli/web_dist, which web_server.py serves.
COPY --chown=root:root scripts/patch-chat-attachments.py /opt/render-tools/patch-chat-attachments.py
RUN python3 /opt/render-tools/patch-chat-attachments.py /opt/hermes \
 && /opt/hermes/.venv/bin/python -c "import ast,pathlib; ast.parse(pathlib.Path('/opt/hermes/hermes_cli/web_server.py').read_text()); ast.parse(pathlib.Path('/opt/hermes/hermes_cli/web_models.py').read_text())" \
 && cd /opt/hermes \
 && npm run build --workspace web \
 && test -f /opt/hermes/hermes_cli/web_dist/index.html

# Boot-time config patch, wired in as an s6-overlay cont-init hook.
#
# Numbered 016- so it lands after the upstream 01-hermes-setup hook (which
# seeds and chowns config.yaml) and before 02-reconcile-profiles starts a
# gateway. Every cont-init.d hook finishes before s6-rc starts main-hermes
# and dashboard, so the patch is applied before anything reads the config.
COPY --chown=root:root scripts/patch-config.py /opt/render-tools/patch-config.py
COPY --chmod=0755 scripts/cont-init-patch-config.sh /etc/cont-init.d/016-render-patch-config
RUN chmod 0755 /opt/render-tools/patch-config.py

# Kanban SDLC infrastructure (coder -> PR -> reviewer -> squash-merge):
#
#   - plugins/render-kanban-guard/ -> /opt/hermes/plugins/ (the bundled-source
#     plugin root: hermes_cli/plugins.py:76-86 get_bundled_plugins_dir() =
#     <repo>/plugins unless HERMES_BUNDLED_PLUGINS overrides it). Bundled, not
#     under /opt/data/plugins, so the in-gateway dispatcher AND every
#     dispatcher-spawned worker (each with its own HERMES_HOME under
#     /opt/data/profiles/<name>) resolve the same copy; it is still opt-in
#     per profile via plugins.enabled (patch-config.py writes that).
#   - scripts/git-hooks/pre-push -> /opt/render-tools/git-hooks/ (0755). The
#     017 hook points the anchor repo's core.hooksPath here, which every
#     .worktrees/* checkout inherits. Accident guard, not a security boundary.
#   - scripts/kanban-health.py -> /opt/render-tools/scripts/ (read-only image
#     copy; 017 installs it into /opt/data/scripts once, where `hermes cron
#     --script` can see it, and never overwrites operator edits).
#   - 017-render-kanban-bootstrap: numbered after 016 (root config patched
#     first) and before upstream 02-reconcile-profiles (which walks
#     /opt/data/profiles/* to recreate s6 gateway slots), so the coder /
#     reviewer profiles exist before anything enumerates them. Every step in
#     it is `|| true`; it never fails the boot.
#
# The trailing RUN re-asserts perms (COPY --chmod covers the files, install -d
# the dirs) and fails the BUILD -- not the boot -- on a syntax error in either
# shell script or the health probe, mirroring the ast.parse checks above.
RUN install -d -o root -g root -m 0755 /opt/render-tools/git-hooks /opt/render-tools/scripts
COPY --chown=root:root plugins/render-kanban-guard/ /opt/hermes/plugins/render-kanban-guard/
COPY --chmod=0755 scripts/git-hooks/pre-push /opt/render-tools/git-hooks/pre-push
COPY --chmod=0755 scripts/kanban-health.py /opt/render-tools/scripts/kanban-health.py
COPY --chmod=0755 scripts/cont-init-kanban-bootstrap.sh /etc/cont-init.d/017-render-kanban-bootstrap
RUN chmod 0755 /opt/render-tools/git-hooks /opt/render-tools/scripts \
      /opt/render-tools/git-hooks/pre-push /opt/render-tools/scripts/kanban-health.py \
      /etc/cont-init.d/017-render-kanban-bootstrap \
 && chmod -R a+rX /opt/hermes/plugins/render-kanban-guard \
 && sh -n /etc/cont-init.d/017-render-kanban-bootstrap \
 && sh -n /opt/render-tools/git-hooks/pre-push \
 && /opt/hermes/.venv/bin/python -c "import ast,pathlib; ast.parse(pathlib.Path('/opt/render-tools/scripts/kanban-health.py').read_text())" \
 && test -f /opt/hermes/plugins/render-kanban-guard/plugin.yaml

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