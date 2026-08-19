#!/usr/bin/env python3
"""Build-time patch adding reliable dashboard chat attachments on Hermes v2026.8.18
(first shipped against v2026.8.3; server-side anchors re-based on upgrade).

The patch has two parts:
1. raw authenticated fetches redirect to login on a structured gated-mode 401
   instead of painting the JSON "Unauthorized" response in red;
2. the Chat page gains a paperclip plus general file drop support. Images keep
   using /image; documents are cached and their absolute path is inserted into
   the active TUI message.

Idempotent and deliberately fails if an upstream anchor changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "_RENDER_TOOLS_CHAT_ATTACHMENTS_PATCH"
TARGETS = (
    "hermes_cli/web_models.py",
    "hermes_cli/web_server.py",
    "web/src/lib/api.ts",
    "web/src/lib/chatImagePaste.ts",
    "web/src/pages/ChatPage.tsx",
)


def replace_once(path: Path, source: str, anchor: str, replacement: str, label: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(f"{path}: {label}: expected one anchor, found {count}")
    return source.replace(anchor, replacement)


def patch_models(path: Path, source: str) -> str:
    anchor = '''class ChatImageUpload(BaseModel):
    data_url: str
    filename: Optional[str] = None


class ManagedDirectoryCreate(BaseModel):'''
    replacement = '''class ChatImageUpload(BaseModel):
    data_url: str
    filename: Optional[str] = None


class ChatFileUpload(BaseModel):
    data_url: str
    filename: Optional[str] = None


class ManagedDirectoryCreate(BaseModel):'''
    return replace_once(path, source, anchor, replacement, "ChatFileUpload model")


def patch_server(path: Path, source: str) -> str:
    source = replace_once(
        path,
        source,
        "    ChatImageUpload,\n    ManagedDirectoryCreate,",
        "    ChatImageUpload,\n    ChatFileUpload,\n    ManagedDirectoryCreate,",
        "model import",
    )
    # Upstream (v2026.8.18) runs the image-upload body inside a nested
    # ``_run()`` that is dispatched via ``asyncio.to_thread``; the return dict
    # is therefore indented one level deeper than it was on v2026.8.3, and is
    # followed by the to_thread comment block. Two anchors: the dict itself
    # (to tag the image kind) and the handler's tail (to append our route).
    source = replace_once(
        path,
        source,
        '''            "mime_type": mime_type,
        }

    # _profile_scope acquires _SKILLS_PROFILE_LOCK''',
        '''            "mime_type": mime_type,
            "kind": "image",
        }

    # _profile_scope acquires _SKILLS_PROFILE_LOCK''',
        "image upload kind",
    )
    anchor = '''    return await asyncio.to_thread(_run)


@app.get("/api/files")'''
    replacement = '''    return await asyncio.to_thread(_run)


@app.post("/api/chat/file-upload")
async def upload_chat_file(payload: ChatFileUpload, profile: Optional[str] = None):
    """Cache a browser-provided document and return an agent-visible path.

    [render-tools] Mirrors ``upload_chat_image`` above: the body runs in a
    worker thread because ``_profile_scope`` takes a lock and the document
    cache does file I/O.
    """
    def _run():
        data, mime_type = _decode_data_url(payload.data_url)
        filename = Path(_sanitize_chat_image_filename(payload.filename)).name
        if not filename:
            filename = "attachment.bin"
        with _profile_scope(profile):
            from gateway.platforms.base import cache_document_from_bytes
            try:
                target = cache_document_from_bytes(data, filename)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=f"Could not cache attachment: {exc}")
        return {
            "ok": True,
            "path": str(target),
            "name": Path(target).name,
            "bytes": len(data),
            "mime_type": mime_type,
            "kind": "document",
            "marker": "_RENDER_TOOLS_CHAT_ATTACHMENTS_PATCH",
        }

    return await asyncio.to_thread(_run)


@app.get("/api/files")'''
    return replace_once(path, source, anchor, replacement, "file upload route")


def patch_api(path: Path, source: str) -> str:
    anchor = '''  return fetch(`${BASE}${url}`, {
    ...init,
    headers,
    credentials: init?.credentials ?? "include",
  });
}'''
    replacement = '''  const res = await fetch(`${BASE}${url}`, {
    ...init,
    headers,
    credentials: init?.credentials ?? "include",
  });
  if (res.status === 401) {
    try {
      const body = (await res.clone().json()) as {
        error?: string;
        login_url?: string;
      };
      if (
        (body.error === "unauthenticated" || body.error === "session_expired") &&
        body.login_url
      ) {
        sessionStorage.setItem(
          "hermes.lastLocation",
          window.location.pathname + window.location.search,
        );
        window.location.assign(body.login_url);
      }
    } catch {
      // Non-JSON/domain-level 401: let the caller report it normally.
    }
  }
  return res;
}'''
    return replace_once(path, source, anchor, replacement, "authedFetch 401 handling")


def patch_upload_lib(path: Path, source: str) -> str:
    source = source.replace("export interface ChatImageUploadResult {", "export interface ChatImageUploadResult {")
    source = replace_once(
        path,
        source,
        "  mime_type: string;\n}",
        '  mime_type: string;\n  kind?: "image" | "document";\n}',
        "upload result kind",
    )
    append = r'''

/** Upload an image or document and return its server-side agent-visible path. */
export async function uploadChatFile(
  file: File,
  profile = "",
): Promise<ChatImageUploadResult> {
  if (file.type.startsWith("image/")) return uploadChatImage(file, profile);
  if (file.size === 0) throw new Error("attachment is empty");
  const maxBytes = 100 * 1024 * 1024;
  if (file.size > maxBytes) throw new Error("attachment too large (max 100 MB)");
  const dataUrl = await fileToDataUrl(file);
  const qs = profile ? `?profile=${encodeURIComponent(profile)}` : "";
  const res = await authedFetch(`/api/chat/file-upload${qs}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl, filename: file.name }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text || `HTTP ${res.status}`);
  }
  const uploaded = (await res.json()) as ChatImageUploadResult;
  if (!uploaded?.path) throw new Error("attachment upload did not return a path");
  return uploaded;
}

/** Pull all browser files from a paste/drop transfer, deduplicated. */
export function filesFromTransfer(data: DataTransfer | null): File[] {
  if (!data) return [];
  const files: File[] = [];
  const seen = new Set<string>();
  const add = (file: File | null) => {
    if (!file) return;
    const key = imageFileKey(file);
    if (seen.has(key)) return;
    seen.add(key);
    files.push(file);
  };
  if (data.items?.length) {
    for (let i = 0; i < data.items.length; i++) {
      const item = data.items[i];
      if (item.kind === "file") add(item.getAsFile());
    }
  }
  if (data.files?.length) {
    for (let i = 0; i < data.files.length; i++) add(data.files[i]);
  }
  return files;
}

export function transferMayContainFiles(data: DataTransfer | null): boolean {
  if (!data) return false;
  if (data.files?.length) return true;
  if (data.items?.length) {
    for (let i = 0; i < data.items.length; i++) {
      if (data.items[i].kind === "file") return true;
    }
  }
  return false;
}
'''
    if not source.endswith("\n"):
        source += "\n"
    return source + append


def patch_chat_page(path: Path, source: str) -> str:
    source = replace_once(
        path,
        source,
        'import { Copy, PanelRight, RotateCcw, X } from "lucide-react";',
        'import { Copy, PanelRight, Paperclip, RotateCcw, X } from "lucide-react";',
        "Paperclip import",
    )
    source = replace_once(
        path,
        source,
        '''  imageFilesFromTransfer,
  transferMayContainImage,
  uploadChatImage,''',
        '''  filesFromTransfer,
  transferMayContainFiles,
  uploadChatFile,''',
        "attachment imports",
    )
    source = replace_once(
        path,
        source,
        "  const hostRef = useRef<HTMLDivElement | null>(null);\n",
        "  const hostRef = useRef<HTMLDivElement | null>(null);\n  const fileInputRef = useRef<HTMLInputElement | null>(null);\n",
        "file input ref",
    )
    source = source.replace("imageFilesFromTransfer(ev.clipboardData)", "filesFromTransfer(ev.clipboardData)")
    source = source.replace("imageFilesFromTransfer(ev.dataTransfer)", "filesFromTransfer(ev.dataTransfer)")
    source = source.replace("transferMayContainImage(ev.dataTransfer)", "transferMayContainFiles(ev.dataTransfer)")
    source = source.replace("uploadAndAttachImages", "uploadAndAttachFiles")
    source = source.replace("uploadChatImage(file, scopedProfile)", "uploadChatFile(file, scopedProfile)")
    source = replace_once(
        path,
        source,
        '''          paths.push(uploaded.path);
        }
        await driveImageAttach(paths);''',
        '''          if (uploaded.kind === "document") {
            const ws = wsRef.current;
            if (!ws || ws.readyState !== WebSocket.OPEN) {
              setBanner("Attachment uploaded, but chat is not connected — try again.");
              return;
            }
            ws.send(`Attached file available at ${uploaded.path}. `);
          } else {
            paths.push(uploaded.path);
          }
        }
        await driveImageAttach(paths);''',
        "document path injection",
    )
    source = replace_once(
        path,
        source,
        '''      <PluginSlot name="chat:top" />
      {mobileModelToolsPortal}''',
        '''      <PluginSlot name="chat:top" />
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? []);
          if (files.length) {
            const host = hostRef.current;
            const transfer = new DataTransfer();
            files.forEach((file) => transfer.items.add(file));
            host?.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, bubbles: true }));
          }
          event.currentTarget.value = "";
        }}
      />
      {mobileModelToolsPortal}''',
        "hidden file input",
    )
    source = replace_once(
        path,
        source,
        '''          <Button
            ghost
            onClick={handleCopyLast}''',
        '''          <Button
            ghost
            onClick={() => fileInputRef.current?.click()}
            title="Attach images, PDFs, documents, spreadsheets, or other files"
            aria-label="Attach files"
            className={cn(
              "absolute z-10 bottom-2 left-2 sm:bottom-3 sm:left-3 lg:bottom-4 lg:left-4",
              "rounded border border-current/30 bg-black/20 px-2 py-1 text-xs",
              "opacity-70 hover:opacity-100 hover:border-current/60",
            )}
            style={{ color: terminalFg }}
          >
            <span className="inline-flex items-center gap-1.5">
              <Paperclip className="h-3 w-3 shrink-0" />
              <span className="hidden min-[400px]:inline tracking-wide">attach files</span>
            </span>
          </Button>

          <Button
            ghost
            onClick={handleCopyLast}''',
        "paperclip button",
    )
    return source


PATCHERS = {
    "hermes_cli/web_models.py": patch_models,
    "hermes_cli/web_server.py": patch_server,
    "web/src/lib/api.ts": patch_api,
    "web/src/lib/chatImagePaste.ts": patch_upload_lib,
    "web/src/pages/ChatPage.tsx": patch_chat_page,
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-chat-attachments.py <hermes-source-root>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    server = root / "hermes_cli/web_server.py"
    try:
        if MARKER in server.read_text(encoding="utf-8"):
            print(f"[render-tools] {root} already carries the chat attachments patch")
            return 0
        staged: dict[Path, str] = {}
        for rel, patcher in PATCHERS.items():
            path = root / rel
            staged[path] = patcher(path, path.read_text(encoding="utf-8"))
        for path, text in staged.items():
            path.write_text(text, encoding="utf-8")
    except (OSError, RuntimeError) as exc:
        print(f"[render-tools] chat attachments patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"[render-tools] patched {root}: authenticated unified chat attachments enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
