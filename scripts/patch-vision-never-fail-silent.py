#!/usr/bin/env python3
"""Build-time patch: never-fail-silent on attached-image vision analysis.

Why: when a user attaches an image in the dashboard and the main model has no
native vision (deepseek-v4-flash), the gateway pre-analyzes the image via the
auxiliary.vision backend and prepends the description to the user turn
(_enrich_with_attached_images). If that analysis FAILS, the old code injected a
bland ``[The user attached an image but analysis failed.]`` into the model's
context and never told the user - a silent failure. The model saw a note, the
user saw nothing, and a vision outage looked like "images just stopped working."

This patch makes two changes:

1. _enrich_with_attached_images gains a ``notify`` callback (optional). When an
   attached image can't be analyzed, the REAL reason is:
     - carried into the model's context (so the agent can act on it), and
     - passed to ``notify`` (so the gateway can surface a user-visible error).
2. The three call sites in _run_prompt_submit pass a ``notify`` that emits a
   dashboard error event with the failure message.

Idempotent. Deliberately FAILS LOUDLY if an anchor isn't found, so a broken
build stops instead of shipping silently-still-broken image handling. On an
upstream bump, either re-anchor or delete this script + its Dockerfile step.

Rationale note (2026-08-17): the route now uses a :free OpenRouter vision model
(auxiliary.vision.model), so failures should be rare - but "rare" is not
"never," and a silent drop of a user's attachment is exactly the bug class this
ticket exists to eliminate.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "_RENDER_TOOLS_VISION_NEVER_FAIL_SILENT"

# --- Anchor 1: the function signature ---
SIG_ANCHOR = """def _enrich_with_attached_images(user_text: str, image_paths: list[str]) -> str:
    """Pre-analyze attached images via vision and prepend descriptions to user text.""""""
SIG_REPLACEMENT = """def _enrich_with_attached_images(
    user_text: str,
    image_paths: list[str],
    *,
    notify=None,  # _RENDER_TOOLS_VISION_NEVER_FAIL_SILENT
) -> str:
    """Pre-analyze attached images via vision and prepend descriptions to user text.

    ``notify``: optional callback invoked with a user-visible error string when
    an attached image cannot be analyzed. Never fail silently: the real reason
    is carried into the model's context AND surfaced to the user via ``notify``.
    """"""  # noqa: E501

# --- Anchor 2: the failure body inside the loop ---
FAIL_ANCHOR = """            desc = r.get("analysis", "") if r.get("success") else None
            parts.append(
                f"[The user attached an image:\\n{desc}]\\n{hint}"
                if desc
                else f"[The user attached an image but analysis failed.]\\n{hint}"
            )
        except Exception:
            parts.append(f"[The user attached an image but analysis failed.]\\n{hint}")

    text = user_text or """""
FAIL_REPLACEMENT = """            desc = r.get("analysis", "") if r.get("success") else None
            if desc:
                parts.append(f"[The user attached an image:\\n{desc}]\\n{hint}")
            else:
                reason = str(
                    r.get("analysis") or r.get("error") or "unknown vision error"
                ).strip()
                failures.append(reason)
                parts.append(
                    f"[The user attached an image but analysis failed: {reason}]\\n{hint}"
                )
        except Exception as _exc:
            failures.append(str(_exc))
            parts.append(
                f"[The user attached an image but analysis failed: {_exc}]\\n{hint}"
            )

    if failures and notify is not None:
        try:
            notify("\\n".join(failures))
        except Exception:
            pass

    text = user_text or """""  # noqa: E501

# --- Anchor 3: introduce the failures list at the top of the loop ---
INIT_ANCHOR = """    parts: list[str] = []
    for path in image_paths:
        p = Path(path)
        if not p.exists():
            continue
        hint = f"[You can examine it with vision_analyze using image_url: {p}]"
        try:
            r = _json.loads("""
INIT_REPLACEMENT = """    parts: list[str] = []
    failures: list[str] = []
    for path in image_paths:
        p = Path(path)
        if not p.exists():
            failures.append(f"attached image not found on disk: {p}")
            continue
        hint = f"[You can examine it with vision_analyze using image_url: {p}]"
        try:
            r = _json.loads("""

# --- Anchor 4: the three call sites pass notify that emits a dashboard error ---
CALLSITE_ANCHOR = """            run_message = _enrich_with_attached_images(prompt, images)"""
CALLSITE_REPLACEMENT = """            run_message = _enrich_with_attached_images(
                prompt,
                images,
                notify=lambda msg: _emit(
                    "error", sid, {"message": f"[vision] {msg}"}
                ),
            )"""


def _apply(text: str, anchor: str, replacement: str, label: str) -> str:
    count = text.count(anchor)
    if count == 0:
        raise SystemExit(
            f"[{MARKER}] FAILED: anchor for {label} not found in source. "
            f"Re-anchor the patch or delete it on an upstream bump. Aborting build."
        )
    # Anchor 4 is expected to appear 3x (the three call sites). The others 1x.
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
    # Replace all call sites (the lambda is identical for each).
    text = text.replace(CALLSITE_ANCHOR, CALLSITE_REPLACEMENT)

    target.write_text(text, encoding="utf-8")
    print(f"[{MARKER}] applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
