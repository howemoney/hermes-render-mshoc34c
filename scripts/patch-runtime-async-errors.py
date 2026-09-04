#!/usr/bin/env python3
"""Patch two hosted runtime async-context bugs in Hermes v2026.8.31.

Fails loudly if upstream changes either anchor. Remove after the fixes ship in
the pinned upstream image.
"""
from __future__ import annotations

import sys
from pathlib import Path

MCP_MARKER = "_RENDER_TOOLS_MCP_WATCH_COROUTINE_PATCH"
LANGFUSE_MARKER = "_RENDER_TOOLS_LANGFUSE_CONTEXT_PATCH"

MCP_ANCHOR = '''                    _call_coro = server.session.call_tool(tool_name, arguments=args)
                    _watch_children = getattr(server, "_watch_stdio_children", None)
                    _watch_ok = (
                        _watch_children is not None
                        and inspect.isawaitable(_watch_children())
                        and asyncio.iscoroutine(_call_coro)
                    )
'''
MCP_REPLACEMENT = '''                    _call_coro = server.session.call_tool(tool_name, arguments=args)
                    _watch_children = getattr(server, "_watch_stdio_children", None)
                    # Instantiate the watcher exactly once. The old awaitability
                    # probe called the async function and discarded that coroutine,
                    # producing RuntimeWarning and retaining coroutine state.
                    _watch_coro = (
                        _watch_children() if callable(_watch_children) else None
                    )  # _RENDER_TOOLS_MCP_WATCH_COROUTINE_PATCH
                    _watch_ok = (
                        inspect.isawaitable(_watch_coro)
                        and asyncio.iscoroutine(_call_coro)
                    )
'''
MCP_TASK_ANCHOR = "                        watch_task = asyncio.ensure_future(_watch_children())\n"
MCP_TASK_REPLACEMENT = "                        watch_task = asyncio.ensure_future(_watch_coro)\n"

LANGFUSE_ANCHOR = '''    if propagate_attributes is not None:
        try:
            with propagate_attributes(
                session_id=session_id or task_key,
                trace_name="Hermes turn",
                tags=["hermes", "langfuse"],
            ):
                root_ctx = client.start_as_current_observation(
                    trace_context=trace_ctx,
                    name="Hermes turn",
                    as_type="chain",
                    input=trace_input,
                    metadata=metadata,
                    end_on_exit=False,
                )
                root_span = root_ctx.__enter__()
        except Exception:
            root_ctx = client.start_as_current_observation(
                trace_context=trace_ctx,
                name="Hermes turn",
                as_type="chain",
                input=trace_input,
                metadata=metadata,
                end_on_exit=False,
            )
            root_span = root_ctx.__enter__()
    else:
        root_ctx = client.start_as_current_observation(
            trace_context=trace_ctx,
            name="Hermes turn",
            as_type="chain",
            input=trace_input,
            metadata=metadata,
            end_on_exit=False,
        )
        root_span = root_ctx.__enter__()
'''
LANGFUSE_REPLACEMENT = '''    # Do not keep an OpenTelemetry current-observation context manager open
    # across a whole gateway turn. The turn can resume/finalize in a different
    # asyncio Context, where __exit__ cannot reset the original ContextVar token.
    # A non-current root observation still parents children explicitly through
    # root_span.start_observation and carries the same trace/session metadata.
    root_ctx = None  # _RENDER_TOOLS_LANGFUSE_CONTEXT_PATCH
    root_span = client.start_observation(
        trace_context=trace_ctx,
        name="Hermes turn",
        as_type="chain",
        input=trace_input,
        metadata=metadata,
    )
'''


def replace_exact(source: str, anchor: str, replacement: str, label: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    return source.replace(anchor, replacement)


def patch_mcp(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    if MCP_MARKER in source:
        print(f"{path}: MCP patch already applied")
        return
    source = replace_exact(source, MCP_ANCHOR, MCP_REPLACEMENT, "MCP watcher probe")
    source = replace_exact(source, MCP_TASK_ANCHOR, MCP_TASK_REPLACEMENT, "MCP watcher task")
    path.write_text(source, encoding="utf-8")
    print(f"{path}: patched discarded MCP watcher coroutine")


def patch_langfuse(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    if LANGFUSE_MARKER in source:
        print(f"{path}: Langfuse patch already applied")
        return
    source = replace_exact(source, LANGFUSE_ANCHOR, LANGFUSE_REPLACEMENT, "Langfuse root context")
    path.write_text(source, encoding="utf-8")
    print(f"{path}: patched cross-task OpenTelemetry context")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: patch-runtime-async-errors.py <mcp_tool.py> <langfuse/__init__.py>", file=sys.stderr)
        return 2
    patch_mcp(Path(sys.argv[1]))
    patch_langfuse(Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
