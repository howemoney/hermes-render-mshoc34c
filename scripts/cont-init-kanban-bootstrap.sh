#!/command/with-contenv sh
# s6-overlay cont-init hook: bootstrap the kanban SDLC roles on every boot.
#
# Installed by the Dockerfile as /etc/cont-init.d/017-render-kanban-bootstrap.
# /etc/cont-init.d/* runs in lexicographic order, so this lands after the
# upstream 01-hermes-setup hook (seeds + chowns /opt/data), after upstream
# 015-supervise-perms, after our own 016-render-patch-config (root
# config.yaml: kanban.* topology, plugins.enabled, auxiliary models), and
# BEFORE upstream 02-reconcile-profiles (which recreates the per-profile s6
# gateway slots from /opt/data/profiles/*). Every cont-init hook finishes
# before s6-rc starts main-hermes, so the dispatcher embedded in the gateway
# sees the coder/reviewer profiles, the board workdir, and a fresh anchor on
# its very first tick.
#
# What it does (each step independent, each step `|| true`):
#   (a) profiles `coder` + `reviewer`: create if missing, seed a role SOUL.md,
#       then run patch-config.py --profile-config on each (model pins, skill
#       dirs, plugin enablement -- see scripts/patch-config.py).
#   (b) profile descriptions: the kanban decomposer routes cards by reading
#       each profile's description (hermes_cli/subcommands/profile.py:57-62),
#       so fill in `default`/`coder`/`reviewer` when empty. Never overwrite a
#       description the operator wrote.
#   (c) board `default` gets default_workdir=/opt/data/work/stopsargassum so
#       dashboard-created cards inherit the anchor repo (board metadata, NOT
#       config.yaml -- kanban_db.py:871-908 write_board_metadata).
#   (d) the anchor repo: core.hooksPath -> our pre-push guard, a git identity,
#       and `gh` as the https credential helper. The boot-time `git fetch` +
#       fast-forward that used to be here has been REMOVED: it ran before the
#       gateway's credential broker was available, so it could never authenticate
#       and only produced a misleading warning. The authenticated anchor sync is
#       now performed exclusively by the render-kanban-guard plugin's
#       kanban_task_claimed hook, which runs inside the live gateway immediately
#       before each worktree is cut. No `--prune`: pruning origin/wt/*
#       between the reviewer's branch delete and kanban_complete makes
#       upstream's worktree cleanup think commits are unpushed and leak the
#       worktree.
#   (e) /opt/data/scripts/kanban-health.py (cron-visible copy of the health
#       probe; never overwrites an operator-edited one) + /opt/data/.npm-cache
#       (the worker GATE sets npm_config_cache there).
#
# Why a boot hook and not the Dockerfile: everything here lives on the
# persistent /opt/data disk (profiles, board.json, the anchor clone), which
# the image never sees at build time and which survives redeploys. Why not the
# plugin: the plugin runs inside the gateway / workers as the hermes user and
# cannot guarantee it runs before the dispatcher's first tick.
#
# Invariants:
#   - Runs as root (cont-init), but every hermes-owned write goes through
#     s6-setuidgid (same pattern as 016-render-patch-config) so nothing under
#     /opt/data ends up root-owned and unreadable to the gateway (uid 10000).
#   - NEVER fails the boot: set -eu guards our own typos, every step is
#     `|| true` with a "[render-tools]" log line, and the script exits 0.
#   - Bounded: every CLI step is wrapped in `timeout` (coreutils,
#     Essential on the debian:13.4 base -- hermes-v818/Dockerfile:61). No
#     network calls remain at boot time; the anchor fetch was moved to the
#     claim-time plugin. First boot adds two profile creates (<=40 s each).
#   - Idempotent: every write is guarded by a "missing / unset / clean" check,
#     so rebooting N times produces the same disk state as booting once.
#   - No secrets printed: GH_TOKEN is only ever consumed by `gh`, never echoed.

set -eu

# The RENDER_TOOLS_DIR / HERMES_VENV_PYTHON / HERMES_BIN overrides exist so
# tests/test_dockerfile_wiring.py can dry-run this script against a temp tree
# with a fake `hermes`; in the container they are unset and the defaults are
# the baked paths. (HERMES_BIN is also the name upstream's dispatcher honours,
# kanban_db.py:10583-10586, so an operator override stays consistent.)
DATA_DIR="${HERMES_HOME:-/opt/data}"
TOOLS_DIR="${RENDER_TOOLS_DIR:-/opt/render-tools}"
PATCHER="${TOOLS_DIR}/patch-config.py"
HOOKS_DIR="${TOOLS_DIR}/git-hooks"
ANCHOR="${DATA_DIR}/work/stopsargassum"
ANCHOR_REMOTE="origin"
ANCHOR_BRANCH="main"
BOARD="default"
BOT_NAME="Howe Agency Bot"
BOT_EMAIL="snhowe@gmail.com"
VENV_PY="${HERMES_VENV_PYTHON:-/opt/hermes/.venv/bin/python}"

log()  { echo "[render-tools] kanban-bootstrap: $*"; }
warn() { echo "[render-tools] kanban-bootstrap: warning: $*" >&2; }

# The gateway's main-wrapper.sh exports HOME=/opt/data for the hermes user
# (hermes-v818/docker/main-wrapper.sh:65; useradd -d /opt/data at
# Dockerfile:150). s6-setuidgid does NOT rewrite HOME, so set it here: git
# then reads the same ~/.gitconfig the workers will, and `gh` finds the same
# config dir. Harmless for the root-side steps below (mkdir/chown only).
export HOME="${DATA_DIR}"
# Never let a credential prompt hang the boot if GH_TOKEN is missing.
export GIT_TERMINAL_PROMPT=0

# ---------------------------------------------------------------------------
# Privilege drop (verbatim pattern from 016-render-patch-config).
# ---------------------------------------------------------------------------
#
# RENDER_TOOLS_DROP is a test-only override (same family as RENDER_TOOLS_DIR /
# HERMES_BIN below): when it is SET -- even to the empty string -- it is used
# verbatim instead of probing for s6-setuidgid. The in-image test run executes
# this script as root against a root-owned temp tree, where dropping to
# `hermes` would make every write fail for reasons unrelated to the script.
# Real boots never set it.
if [ -n "${RENDER_TOOLS_DROP+x}" ]; then
  DROP="${RENDER_TOOLS_DROP}"
elif command -v s6-setuidgid >/dev/null 2>&1; then
  DROP="s6-setuidgid hermes"
elif [ -x /command/s6-setuidgid ]; then
  DROP="/command/s6-setuidgid hermes"
else
  warn "s6-setuidgid not found; running hermes-side steps as root"
  DROP=""
fi
# NB: `timeout` execs a program and cannot run a shell function, so the
# bounded steps below spell out `timeout N ${DROP} cmd` instead of
# `timeout N as_hermes cmd`. s6-setuidgid execs into its command (same PID),
# so timeout's SIGTERM reaches the real process.
# shellcheck disable=SC2086
as_hermes() { ${DROP} "$@"; }

# `hermes` CLI: the venv binary by absolute path (what the /opt/hermes/bin
# shim execs into anyway); PATH lookup as a fallback. An explicit HERMES_BIN
# is authoritative: if it is set but not executable we treat hermes as
# absent rather than silently falling back to the baked binary (that is what
# lets the "no hermes on this box" path be exercised inside the image).
if [ -n "${HERMES_BIN:-}" ]; then
  if [ -x "${HERMES_BIN}" ]; then
    HERMES="${HERMES_BIN}"
  else
    HERMES=""
    warn "hermes CLI not found at HERMES_BIN=${HERMES_BIN}; profile/board steps will be skipped"
  fi
elif [ -x /opt/hermes/.venv/bin/hermes ]; then
  HERMES="/opt/hermes/.venv/bin/hermes"
elif command -v hermes >/dev/null 2>&1; then
  HERMES="$(command -v hermes)"
else
  HERMES=""
  warn "hermes CLI not found; profile/board steps will be skipped"
fi

# ---------------------------------------------------------------------------
# (a) profiles coder + reviewer
# ---------------------------------------------------------------------------
# Profile dir = <root>/profiles/<name> (hermes_cli/profiles.py:271-286,
# 374-379). `hermes profile create NAME --no-alias --description TEXT` is
# verified at hermes_cli/subcommands/profile.py:28-62; --no-alias skips the
# ~/.local/bin wrapper (useless in the container), and we deliberately do NOT
# pass --no-skills: the reviewer needs the bundled `sdlc-review` skill in its
# own <profile>/skills (the dispatcher force-loads it on review runs,
# kanban_db.py:10379-10385, and skill lookup is HERMES_HOME/skills +
# external_dirs, tools/skills_tool.py:140-144).
#
# SOUL.md: create_profile seeds upstream's DEFAULT_SOUL_MD into every new
# profile (profiles.py:1165-1172; text at hermes_cli/default_soul.py:3-11),
# so "write only if missing" would never fire. Rule used instead: write our
# role statement when the file is missing, empty, or still the untouched
# upstream default (first line matches). Anything else is an operator edit
# and is left alone.

write_soul() {
  # $1 = path; stdin = content. Written as hermes so the owner is right.
  as_hermes sh -c 'cat > "$1"' sh "$1"
}

soul_is_default_or_empty() {
  # true when $1 is missing / empty / byte-for-byte upstream default persona.
  [ -s "$1" ] || return 0
  head -n1 "$1" | grep -q '^You are Hermes Agent, an intelligent AI assistant created by Nous Research' && return 0
  return 1
}

describe_coder="Code worker: implements exactly one kanban card in an isolated git worktree, runs the full test gate, pushes a wt/<task-id> branch and opens a PR, then hands off with kanban_request_review. Never pushes main, never merges."
describe_reviewer="Reviews PRs: runs the gate, waits for CI, squash-merges when green; requests changes otherwise. Never edits implementation files, never pushes main."
describe_default="Orchestrator and reviewer-of-last-resort: decomposes goals into coder cards"

for P in coder reviewer; do
  pdir="${DATA_DIR}/profiles/${P}"
  created=0
  if [ ! -d "${pdir}" ]; then
    if [ -n "${HERMES}" ]; then
      if [ "$P" = "coder" ]; then desc="${describe_coder}"; else desc="${describe_reviewer}"; fi
      log "creating profile '${P}'"
      if timeout 40 ${DROP} "${HERMES}" profile create "${P}" --no-alias --description "${desc}" >/dev/null 2>&1; then
        created=1
        log "profile '${P}' created at ${pdir}"
      else
        warn "profile create '${P}' failed or timed out (rc=$?); will retry next boot if the dir is still missing"
      fi
    else
      warn "cannot create profile '${P}': hermes CLI missing"
    fi
  fi

  [ -d "${pdir}" ] || continue

  soul="${pdir}/SOUL.md"
  if [ "${created}" = 1 ] || soul_is_default_or_empty "${soul}"; then
    case "$P" in
      coder)
        write_soul "${soul}" <<'EOF' || warn "could not write coder SOUL.md"
You are "coder", the Howe Agency kanban code worker. You implement exactly one kanban card at a time, inside the git worktree the dispatcher hands you, on the branch it gives you.
Your FIRST action on every card is skill_view('kanban-sdlc-worker'); follow that protocol to the letter -- it overrides any generic git advice in the repo's CLAUDE.md.
You never push main, never merge, never force-push, never use --no-verify, and never skip or weaken a test harness to get green.
You finish every card with exactly ONE of kanban_request_review(reviewer="reviewer") or kanban_block. Printing a summary and stopping is a protocol violation.
When requirements are ambiguous or a human decision is needed, kanban_block(kind="needs_input") -- do not guess.
EOF
        ;;
      reviewer)
        write_soul "${soul}" <<'EOF' || warn "could not write reviewer SOUL.md"
You are "reviewer", the Howe Agency kanban reviewer and merger. You review exactly one card's PR at a time, in the implementer's worktree, on the implementer's branch.
Your FIRST action on every card is skill_view('kanban-sdlc-reviewer'); it composes with the bundled sdlc-review skill the dispatcher already loaded for you.
You run the full gate yourself, wait for CI on the PR to go green, and squash-merge with gh. Never merge red, never push main, never force-push, never edit implementation files.
You finish every card with exactly ONE of kanban_complete, kanban_request_changes, or kanban_block. Printing a summary and stopping is a protocol violation.
Escalate human-only decisions with kanban_block(kind="needs_input"); request changes rather than fixing code yourself.
EOF
        ;;
    esac
    log "seeded ${soul}"
  fi

  # Per-profile config (model pins, skills.external_dirs, plugins.enabled).
  # The patcher owns the insert-vs-enforce semantics; see its header.
  if [ -x "${PATCHER}" ]; then
    if ! timeout 30 ${DROP} "${PATCHER}" --profile-config "${pdir}/config.yaml" --profile "${P}"; then
      warn "patch-config --profile-config failed for '${P}'; profile keeps its current config"
    fi
  else
    warn "${PATCHER} missing; skipping profile config for '${P}'"
  fi
done

# ---------------------------------------------------------------------------
# (a2) every OTHER profile must never run the kanban dispatcher
# ---------------------------------------------------------------------------
# The dispatcher singleton lock (<kanban_home>/kanban/.dispatcher.lock) is
# shared by every gateway process on this host, and 02-reconcile-profiles
# auto-starts a gateway for every profile whose last state was "running".
# Whichever gateway wins that lock becomes THE dispatcher and reads kanban.*
# from ITS OWN config.yaml -- not the root config the 016 patcher enforces.
# Observed 2026-08-19: the engine-research profile gateway (no messaging
# platforms, started ~20 s before the root gateway) took the lock and the
# root gateway logged "another gateway already holds the dispatcher lock;
# this gateway will NOT dispatch". Pin dispatch_in_gateway=false on every
# profile we do not own (patch-config's 'other' mode touches nothing else),
# so only the root gateway is ever a candidate. coder/reviewer got the same
# pin in the loop above.
if [ -x "${PATCHER}" ] && [ -d "${DATA_DIR}/profiles" ]; then
  for pdir in "${DATA_DIR}"/profiles/*/; do
    [ -d "${pdir}" ] || continue
    P="$(basename "${pdir}")"
    case "${P}" in
      coder|reviewer) continue ;;
    esac
    if ! timeout 30 ${DROP} "${PATCHER}" --profile-config "${pdir%/}/config.yaml" --profile other; then
      warn "could not pin dispatch_in_gateway=false for profile '${P}' (it may still compete for the dispatcher lock)"
    fi
  done
fi

# ---------------------------------------------------------------------------
# (b) profile descriptions (decomposer roster)
# ---------------------------------------------------------------------------
# `hermes profile describe NAME` prints the description; `--text` overwrites
# it (subcommands/profile.py:69-83; handler hermes_cli/main.py:10266-10337,
# default profile resolves to HERMES_HOME itself at :10300-10303). Because
# --text always overwrites, read first: the description lives in
# <profile_dir>/profile.yaml under `description` (profiles.py:819-846). Read
# with the venv python (has PyYAML); if the file is missing it is definitely
# empty; if it exists but cannot be parsed, leave it alone rather than risk
# clobbering an operator-authored value.

description_is_empty() {
  # $1 = profile dir. Exit 0 when empty/missing, 1 when set or unreadable.
  meta="$1/profile.yaml"
  [ -f "${meta}" ] || return 0
  [ -x "${VENV_PY}" ] || return 1
  "${VENV_PY}" - "${meta}" <<'PY' 2>/dev/null
import sys, yaml
try:
    data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    sys.exit(1)
desc = (data.get("description") or "").strip() if isinstance(data, dict) else ""
sys.exit(0 if not desc else 1)
PY
}

ensure_description() {
  # $1 = profile name, $2 = profile dir, $3 = text
  [ -n "${HERMES}" ] || return 0
  [ -d "$2" ] || return 0
  if description_is_empty "$2"; then
    if timeout 30 ${DROP} "${HERMES}" profile describe "$1" --text "$3" >/dev/null 2>&1; then
      log "set description for profile '$1'"
    else
      warn "profile describe '$1' failed (rc=$?)"
    fi
  fi
}

ensure_description default  "${DATA_DIR}"                   "${describe_default}"  || true
ensure_description coder    "${DATA_DIR}/profiles/coder"    "${describe_coder}"    || true
ensure_description reviewer "${DATA_DIR}/profiles/reviewer" "${describe_reviewer}" || true

# ---------------------------------------------------------------------------
# (c) board default workdir
# ---------------------------------------------------------------------------
# `hermes kanban boards set-default-workdir SLUG PATH` (hermes_cli/kanban.py:
# 321-327 parser, :1427-1444 handler -> kb.write_board_metadata). The value
# persists in <root>/kanban/boards/<slug>/board.json (kanban_db.py:684-696,
# 811-819); <root> is HERMES_HOME because /opt/data is not a profiles/ child
# (hermes_constants.py:183-196). Read the JSON directly (stdlib json) to
# decide "unset"; if that read is impossible, run the setter anyway -- it is
# an idempotent overwrite with the same value.
if [ -e "${ANCHOR}/.git" ] && [ -n "${HERMES}" ]; then
  board_json="${DATA_DIR}/kanban/boards/${BOARD}/board.json"
  need_workdir=1
  if [ -f "${board_json}" ]; then
    if command -v python3 >/dev/null 2>&1; then
      if python3 - "${board_json}" <<'PY' 2>/dev/null
import json, sys
try:
    meta = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(2)
sys.exit(0 if (isinstance(meta, dict) and (meta.get("default_workdir") or "").strip()) else 1)
PY
      then need_workdir=0; fi
    fi
  fi
  if [ "${need_workdir}" = 1 ]; then
    if timeout 30 ${DROP} "${HERMES}" kanban boards set-default-workdir "${BOARD}" "${ANCHOR}" >/dev/null 2>&1; then
      log "board '${BOARD}' default_workdir set to ${ANCHOR}"
    else
      warn "kanban boards set-default-workdir failed (rc=$?)"
    fi
  else
    log "board '${BOARD}' default_workdir already set; leaving it"
  fi
else
  log "anchor ${ANCHOR} not a git checkout yet (or hermes missing); skipping board workdir"
fi

# ---------------------------------------------------------------------------
# (d) anchor repo: hooks, identity, credentials
# ---------------------------------------------------------------------------
# NOTE: the boot-time `git fetch origin main` + `git checkout -B main` that
# used to live here has been REMOVED. It ran during cont-init, before the
# Hermes gateway (and therefore git-credential-hermes-gateway) was available,
# so the fetch could never authenticate and always emitted a misleading
# "anchor fetch failed or timed out (offline / GH_TOKEN?)" warning. The
# authenticated anchor sync is now performed exclusively by the
# render-kanban-guard plugin's `kanban_task_claimed` hook
# (plugins/render-kanban-guard/__init__.py:sync_anchor), which runs inside
# the live gateway immediately before each worktree is cut. That is the
# correct timing boundary: the gateway has credentials, the fetch is real,
# and no boot delay or warning noise is produced.
if [ -e "${ANCHOR}/.git" ] && command -v git >/dev/null 2>&1; then
  g() { as_hermes git -C "${ANCHOR}" "$@"; }

  # Hooks: one setting in the shared .git/config covers the anchor and every
  # linked worktree under .worktrees/* (they all resolve core.hooksPath from
  # the same config). Enforced every boot -- it is the guard.
  if [ -x "${HOOKS_DIR}/pre-push" ]; then
    cur_hooks="$(g config --local --get core.hooksPath 2>/dev/null || true)"
    if [ "${cur_hooks}" != "${HOOKS_DIR}" ]; then
      g config --local core.hooksPath "${HOOKS_DIR}" \
        && log "anchor core.hooksPath -> ${HOOKS_DIR}" \
        || warn "could not set core.hooksPath on ${ANCHOR}"
    fi
  else
    warn "${HOOKS_DIR}/pre-push missing or not executable; NOT setting core.hooksPath"
  fi

  # Identity: only fill in what is unset (the operator may have chosen
  # something else on the box; keep it).
  if [ -z "$(g config --get user.name 2>/dev/null || true)" ]; then
    g config --local user.name "${BOT_NAME}" && log "anchor user.name set" || warn "could not set user.name"
  fi
  if [ -z "$(g config --get user.email 2>/dev/null || true)" ]; then
    g config --local user.email "${BOT_EMAIL}" && log "anchor user.email set" || warn "could not set user.email"
  fi

  # Credentials: `gh auth git-credential` serves GH_TOKEN to git over https.
  # Only when no helper is configured anywhere (local/global/system) AND the
  # remote is https -- an ssh remote needs keys, not a token, and an existing
  # helper is an operator decision.
  remote_url="$(g remote get-url "${ANCHOR_REMOTE}" 2>/dev/null || true)"
  if [ -z "$(g config --get-all credential.helper 2>/dev/null || true)" ]; then
    case "${remote_url}" in
      https://*)
        if command -v gh >/dev/null 2>&1; then
          g config --local credential.helper '!gh auth git-credential' \
            && log "anchor credential.helper -> gh auth git-credential" \
            || warn "could not set credential.helper"
        else
          warn "gh not on PATH; leaving credential.helper unset"
        fi
        ;;
      "")  warn "anchor has no '${ANCHOR_REMOTE}' remote; leaving credential.helper unset" ;;
      *)   log "anchor remote is not https (${remote_url%%:*}...); leaving credential.helper unset" ;;
    esac
  fi

  # Anchor sync is deferred to the render-kanban-guard plugin's
  # kanban_task_claimed hook (see header note above). No boot-time fetch.

  # Drop admin entries for worktree dirs that no longer exist (upstream's
  # cleanup removes the dir; a redeploy can race it). Harmless when nothing
  # is stale.
  g worktree prune 2>/dev/null && log "anchor worktree prune ok" || warn "worktree prune failed"
else
  log "anchor ${ANCHOR} not present (or git missing); skipping repo setup -- clone it with 'git clone https://github.com/howemoney/stopsargassum ${ANCHOR}' as hermes"
fi

# ---------------------------------------------------------------------------
# (e) health probe copy + npm cache dir
# ---------------------------------------------------------------------------
# Cron scripts must live under HERMES_HOME/scripts (cron/scheduler.py:
# 3489-3523). The image ships the probe read-only under /opt/render-tools;
# copy it once so `hermes cron create --script kanban-health.py` resolves.
# Never overwrite: an operator may have tuned thresholds on the box.
if as_hermes mkdir -p "${DATA_DIR}/scripts" "${DATA_DIR}/.npm-cache" 2>/dev/null; then
  src="${TOOLS_DIR}/scripts/kanban-health.py"
  dst="${DATA_DIR}/scripts/kanban-health.py"
  if [ -f "${src}" ] && [ ! -e "${dst}" ]; then
    as_hermes cp "${src}" "${dst}" && as_hermes chmod 0755 "${dst}" \
      && log "installed ${dst}" \
      || warn "could not copy kanban-health.py into ${DATA_DIR}/scripts"
  fi
else
  warn "could not create ${DATA_DIR}/scripts / .npm-cache"
fi

# Belt-and-braces when we had to run as root (no s6-setuidgid): hand the
# profile dirs back to hermes so the gateway can read/write them.
if [ -z "${DROP}" ]; then
  for d in "${DATA_DIR}/profiles/coder" "${DATA_DIR}/profiles/reviewer" "${DATA_DIR}/scripts" "${DATA_DIR}/.npm-cache"; do
    [ -e "$d" ] && chown -R hermes:hermes "$d" 2>/dev/null || true
  done
fi

log "done (profiles=coder,reviewer board=${BOARD} anchor=${ANCHOR})"
exit 0
