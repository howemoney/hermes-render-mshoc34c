#!/usr/bin/env python3
"""Build-time patch: never-fail-silent on attached-image vision analysis.

Why: when a user attaches an image in the dashboard and the main model has no
native vision (deepseek-v4-flash), the gateway pre-analyzes the image via the
auxiliary.vision backend and prepends the description to the user turn
(_enrich_with_attached_images). If that analysis FAILS, the old code injected a
bland "[The user attached an image but analysis failed.]" into the model's
context and never told the user - a silent failure.

This patch (the fix/vision-never-fail-silent intent, rebuilt because the
original script had a nested-triple-quote Python syntax error that would crash
the Docker RUN step):

1. _enrich_with_attached_images gains a ``notify`` callback (optional). When an
   attached image can't be analyzed, the REAL reason is:
     - carried into the model's context (so the agent can act on it), and
     - passed to ``notify`` (so the gateway can surface a user-visible error).
2. The call sites in the TUI gateway pass ``notify`` that emits a dashboard
   error event with the failure message.

Idempotent. Fails loudly if an anchor isn't found, so a broken build stops
instead of shipping silently-still-broken image handling. On an upstream bump,
re-anchor or delete this + its Dockerfile step.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "_RENDER_TOOLS_VISION_NEVER_FAIL_SILENT"

_D = "__DOUBLE_QUOTE__"


def _wrapped_doc(text: str) -> str:
    """Return a triple-quoted [[Python string writable at literal-build time]]."""
    return text


# --- Anchor 1: the function signature ------------------------------------
SIG_ANCHOR = ('def _enrich_with_attached_images(user_text: str, image_paths: list[str]) -> str:\n'
              '    """Pre-analyze attached images via vision and prepend descriptions to user text."""')

# Replacement uses single-quoted docstring, then two doubled quote markers for
# the outer string continuation we re-emit by hand.
SIG_REPLACEMENT = (
    'def _enrich_with_attached_images(\n'
    '    user_text: str,\n'
    '    image_paths: list[str],\n'
    '    *,\n'
    '    notify=None,  # _RENDER_TOOLS_VISION_NEVER_FAIL_SILENT\n'
    ') -> str:\n'
    '    """Pre-analyze attached images via vision and prepend descriptions to user text.\n'
    '\n'
    '    ``notify``: optional callback invoked with a user-visible error string when\n'
    '    an attached image cannot be analyzed. Never fail silently: the real reason\n'
    "    is carried into the model's context AND surfaced to the user via ``notify``.\n"
    '    """\n'
)

# --- Anchor 2: the failure body inside the loop ---
FAIL_ANCHOR = (
    '            desc = r.get("analysis", "") if r.get("success") else None\n'
    '            parts.append(\n'
    '                f"[The user attached an image:\\n{desc}]\\n{hint}"\n'
    '                if desc\n'
    '                else f"[The user attached an image but analysis failed.]\\n{hint}"\n'
    '            )\n'
    '        except Exception:\n'
    '            parts.append(f"[The user attached an image but analysis failed.]\\n{hint}")\n'
    '\n'
    '    text = user_text or ""'
)
FAIL_REPLACEMENT = (
    '            desc = r.get("analysis", "") if r.get("success") else None\n'
    '            if desc:\n'
    '                parts.append(f"[The user attached an image:\\n{desc}]\\n{hint}")\n'
    '            else:\n'
    '                reason = str(\n'
    '                    r.get("analysis") or r.get("error") or "unknown vision error"\n'
    '                ).strip()\n'
    '                failures.append(reason)\n'
    '                parts.append(\n'
    '                    f"[The user attached an image but analysis failed: {reason}]\\n{hint}"\n'
    '                )\n'
    '        except Exception as _exc:\n'
    '            failures.append(str(_exc))\n'
    '            parts.append(\n'
    '                f"[The user attached an image but analysis failed: {_exc}]\\n{hint}"\n'
    '            )\n'
    '\n'
    '    if failures and notify is not None:\n'
    '        try:\n'
    '            notify("\\n".join(failures))\n'
    '        except Exception:\n'
    '            pass\n'
    '\n'
    '    text = user_text or ""'
)

# --- Anchor 3: introduce the failures list at the top of the loop ---
INIT_ANCHOR = (
    '    parts: list[str] = []\n'
    '    for path in image_paths:\n'
    '        p = Path(path)\n'
    '        if not p.exists():\n'
    '            continue\n'
    '        hint = f"[You can examine it with vision_analyze using image_url: {p}]"\n'
    '        try:\n'
    '            r = _json.loads('
)
INIT_REPLACEMENT = (
    '    parts: list[str] = []\n'
    '    failures: list[str] = []\n'
    '    for path in image_paths:\n'
    '        p = Path(path)\n'
    '        if not p.exists():\n'
    '            failures.append(f"attached image not found on disk: {p}")\n'
    '            continue\n'
    '        hint = f"[You can examine it with vision_analyze using image_url: {p}]"\n'
    '        try:\n'
    '            r = _json.loads('
)

# --- Anchor 4: the call sites pass notify that emits a dashboard error ---
CALLSITE_ANCHOR = "run_message = _enrich_with_attached_images(prompt, images)"
CALLSITE_REPLACEMENT = (
    "run_message = _enrich_with_attached_images(\n"
    "                prompt,\n"
    "                images,\n"
    "                notify=lambda _m: _emit(\n"
    '                    "error", sid, {"message": "[vision] " + str(_m)}\n'
    "                ),\n"
    "            )"
)


def _apply(text: str, anchor: str, replacement: str, label: str) -> str:
    count = text.count(anchor)
    if count == 0:
        raise SystemExit(
            f"[{MARKER}] FAILED: anchor for {label} not found in source. "
            f"Re-anchor the patch or delete it on an upstream bump. Aborting build."
        )
    return text.replace(anchor, replacement)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <path/to/tui_gateway/server.py>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    text = target.read_text(encoding="utf-8")

    if MARKER in text:
        print(f"[{MARKER}] already applied - no-op")
        return 0

    text = _apply(text, SIG_ANCHOR, SIG_REPLACEMENT, "function signature")
    text = _apply(text, INIT_ANCHOR, INIT_REPLACEMENT, "failures list init")
    text = _apply(text, FAIL_ANCHOR, FAIL_REPLACEMENT, "failure body")
    n = text.count(CALLSITE_ANCHOR)
    if n == 0:
        raise SystemExit(f"[{MARKER}] FAILS: no call sites found. Aborting build.")
    text = text.replace(CALLSITE_ANCHOR, CALLSITE_REPLACEMENT)

    target.write_text(text, encoding="utf-8")
    print(f"[{MARKER}] applied ({n} call-site(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())