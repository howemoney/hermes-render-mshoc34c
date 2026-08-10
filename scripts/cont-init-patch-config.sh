#!/command/with-contenv sh
# s6-overlay cont-init hook: patch /opt/data/config.yaml on every boot.
#
# Installed by the Dockerfile as /etc/cont-init.d/016-render-patch-config,
# so it runs after the upstream 01-hermes-setup hook (which seeds and chowns
# config.yaml) and before 02-reconcile-profiles starts any gateway. All
# cont-init.d hooks complete before s6-rc brings up the main-hermes and
# dashboard services, so the patched config is in place by the time anything
# reads it.
#
# This used to be an ENTRYPOINT wrapper (`tini -g -- bootstrap.sh`) that
# exec'd /opt/hermes/docker/entrypoint.sh at the end. That contract broke in
# base image v2026.8.3: entrypoint.sh became a deprecated shim that runs the
# stage2 bootstrap a second time and never execs the CMD, so `gateway run`
# never started and s6 tore the whole supervision tree down. Upstream's
# guidance is to drop the ENTRYPOINT override entirely and hook in here.
#
# `gosu` is also gone from v2026.8.3 — use s6-overlay's s6-setuidgid, which
# is on PATH inside cont-init.
#
# Never fail the boot on a patch error: the agent still runs without the
# Render MCP server registered, and it can be added from the dashboard.

set -eu

DATA_DIR="${HERMES_HOME:-/opt/data}"
PATCHER="/opt/render-tools/patch-config.py"

if [ ! -x "${PATCHER}" ]; then
  echo "[render-tools] warning: ${PATCHER} not found or not executable; skipping" >&2
  exit 0
fi

# s6 puts /command on PATH for cont-init hooks, but resolve the privilege-drop
# helper explicitly so a PATH surprise degrades to "patch as root" (the file is
# chowned back below) instead of silently skipping the patch entirely.
if command -v s6-setuidgid >/dev/null 2>&1; then
  DROP="s6-setuidgid hermes"
elif [ -x /command/s6-setuidgid ]; then
  DROP="/command/s6-setuidgid hermes"
else
  echo "[render-tools] warning: s6-setuidgid not found; patching as root" >&2
  DROP=""
fi

# 01-hermes-setup has already created and chowned ${DATA_DIR} by now, so we
# only drop to the hermes user to write as its owner.
if ! ${DROP} "${PATCHER}" "${DATA_DIR}/config.yaml"; then
  echo "[render-tools] warning: config patch failed; continuing with unmodified config" >&2
fi

# Belt-and-braces: if the patch ran as root (no s6-setuidgid), make sure the
# gateway can still write its own config afterwards.
if [ -z "${DROP}" ] && [ -f "${DATA_DIR}/config.yaml" ]; then
  chown hermes:hermes "${DATA_DIR}/config.yaml" 2>/dev/null || true
fi

exit 0
