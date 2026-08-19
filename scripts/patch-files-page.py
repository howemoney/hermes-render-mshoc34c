#!/usr/bin/env python3
"""Build-time patch for the dashboard Files page listing on Render.

Upstream bug (observed on nousresearch/hermes-agent:v2026.8.3, still present
at v2026.8.18, dashboard route ``GET /api/files``):

    with os.scandir(target) as scan:
        entries = [
            _managed_file_entry(policy, Path(entry.path))
            for entry in scan
            if not _is_sensitive_path(Path(entry.path))
        ]

(v2026.8.3 spelled the same comprehension over ``target.iterdir()``; the
anchor below tracks the current ``os.scandir`` form.)

``_managed_file_entry()`` resolves every child and raises
``HTTPException(403, "Path outside managed files root")`` when the resolved
path escapes the locked root. On a hosted deploy the locked root is
``/opt/data``, and a SINGLE escaping child -- any symlink on the data disk
pointing at, say, ``/opt/hermes`` or a repo checkout outside the disk --
makes the whole comprehension raise. The Files page then renders nothing but
``Error: 403: {"detail":"Path outside managed files root"}``, and there is no
way to browse or upload anything.

An escaping symlink is a legitimate thing to find in ``/opt/data``: the agent
creates them (worktrees use ``.worktreeinclude`` symlinks, the LSP installer
links binaries), so the listing has to tolerate them.

This patch turns the comprehension into a loop that SKIPS an entry the root
check rejects -- the same treatment ``_is_sensitive_path()`` entries already
get -- and logs which child was skipped, so the offending link is
identifiable from the service logs. It does not widen the sandbox: the
skipped entry is not listed, not readable, and not writable, exactly as
before. Every other 4xx/5xx from ``_managed_file_entry`` still propagates.

Idempotent, and deliberately FAILS LOUDLY: if the anchor is not found the
build stops rather than shipping an image where the Files page is silently
still broken. On an upstream bump, either re-anchor the patch or -- if
upstream has fixed the listing -- delete this script and its Dockerfile step.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "_RENDER_TOOLS_FILES_PATCH"

ANCHOR = """    try:
        with os.scandir(target) as scan:
            entries = [
                _managed_file_entry(policy, Path(entry.path))
                for entry in scan
                if not _is_sensitive_path(Path(entry.path))
            ]
    except PermissionError:
"""

REPLACEMENT = """    # [render-tools] One child that resolves outside the locked root (a
    # symlink out of /opt/data) used to abort the entire listing with a 403.
    # Skip those entries -- and log them -- instead of failing the request.
    try:
        entries = []  # _RENDER_TOOLS_FILES_PATCH
        with os.scandir(target) as scan:
            for entry in scan:
                child = Path(entry.path)
                if _is_sensitive_path(child):
                    continue
                try:
                    entries.append(_managed_file_entry(policy, child))
                except HTTPException as exc:
                    if exc.status_code in (400, 403):
                        _log.warning(
                            "[render-tools] files listing: skipping %s (%s)",
                            child,
                            exc.detail,
                        )
                        continue
                    raise
    except PermissionError:
"""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-files-page.py <path/to/web_server.py>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[render-tools] cannot read {path}: {exc}", file=sys.stderr)
        return 1

    if MARKER in source:
        print(f"[render-tools] {path} already carries the files-listing patch")
        return 0

    count = source.count(ANCHOR)
    if count != 1:
        print(
            f"[render-tools] files-listing patch did not apply: expected exactly "
            f"one match for the /api/files listing block in {path}, found {count}. "
            f"Upstream changed this code -- re-anchor scripts/patch-files-page.py, "
            f"or drop it if upstream now skips entries outside the managed root.",
            file=sys.stderr,
        )
        return 1

    path.write_text(source.replace(ANCHOR, REPLACEMENT), encoding="utf-8")
    print(f"[render-tools] patched {path}: /api/files skips entries outside the root")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
