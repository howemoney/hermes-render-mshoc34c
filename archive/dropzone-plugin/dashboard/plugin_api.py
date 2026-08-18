"""Dropzone dashboard plugin — file upload backend.

Mounted at /api/plugins/dropzone/ by the Hermes dashboard plugin loader
(hermes_cli/web_server.py::_mount_plugin_api_routes).

SECURITY
--------
The dashboard's global auth middleware explicitly SKIPS any path starting
with ``/api/plugins/``::

    if path.startswith("/api/") and path not in _PUBLIC_API_PATHS \
            and not path.startswith("/api/plugins/"):
        ...check token...

This service is bound to 0.0.0.0 with --insecure behind a public URL, so an
unauthenticated upload route would be an arbitrary-file-write primitive for
the entire internet. We therefore re-implement the token check locally and
apply it as a router-level dependency on every route below.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from gateway.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    cache_image_from_bytes,
)

# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

# Per-file cap. Generous enough for scanned PDFs and decks, small enough that
# a hostile client can't fill the 5 GB persistent disk in one request.
MAX_FILE_BYTES = 32 * 1024 * 1024  # 32 MB
MAX_FILES_PER_REQUEST = 10

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Everything the agent can actually open: images (vision_analyze) plus the
# document set the messaging adapters already support (read_file / intake).
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | set(SUPPORTED_DOCUMENT_TYPES)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

SESSION_HEADER = "X-Hermes-Session-Token"


def _expected_token() -> str:
    """Read the dashboard's ephemeral session token at call time.

    Imported lazily rather than at module scope: this plugin file is exec'd
    during web_server import, so a top-level ``from hermes_cli.web_server
    import _SESSION_TOKEN`` would either be circular or capture a stale value.
    """
    try:
        from hermes_cli import web_server

        return getattr(web_server, "_SESSION_TOKEN", "") or ""
    except Exception:  # pragma: no cover - defensive
        return ""


def require_session_token(request: Request) -> None:
    """Mirror web_server._has_valid_session_token for this plugin's routes."""
    expected = _expected_token()
    if not expected:
        # Fail closed. No token configured means we cannot authenticate.
        raise HTTPException(status_code=503, detail="Dashboard token unavailable")

    supplied = request.headers.get(SESSION_HEADER, "")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
    if not supplied:
        supplied = request.query_params.get("token", "")

    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


router = APIRouter(dependencies=[Depends(require_session_token)])


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/limits")
async def limits():
    """Advertise accepted types and size caps so the UI can pre-validate."""
    return {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_files": MAX_FILES_PER_REQUEST,
        "extensions": sorted(ALLOWED_EXTENSIONS),
        "image_extensions": sorted(IMAGE_EXTENSIONS),
    }


@router.post("/upload")
async def upload(files: List[UploadFile] = File(...)):
    """Persist uploaded files into the Hermes media caches.

    Images land in the image cache (so vision_analyze can read them);
    everything else lands in the document cache under its original,
    human-readable filename. Returns absolute paths for the agent to open.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files supplied")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {MAX_FILES_PER_REQUEST})",
        )

    saved = []
    errors = []

    for upload_file in files:
        original = Path(upload_file.filename or "upload").name
        ext = Path(original).suffix.lower()

        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"name": original, "error": f"Unsupported type '{ext or 'none'}'"})
            continue

        data = await upload_file.read()
        if not data:
            errors.append({"name": original, "error": "Empty file"})
            continue
        if len(data) > MAX_FILE_BYTES:
            errors.append({
                "name": original,
                "error": f"Too large ({len(data) // (1024 * 1024)} MB, max {MAX_FILE_BYTES // (1024 * 1024)} MB)",
            })
            continue

        try:
            if ext in IMAGE_EXTENSIONS:
                path = cache_image_from_bytes(data, ext=ext)
                kind = "image"
            else:
                path = cache_document_from_bytes(data, original)
                kind = "document"
        except Exception as exc:
            errors.append({"name": original, "error": str(exc)})
            continue

        saved.append({
            "name": original,
            "path": path,
            "kind": kind,
            "bytes": len(data),
        })

    if not saved and errors:
        raise HTTPException(status_code=400, detail=errors)

    return {"ok": True, "files": saved, "errors": errors}


@router.get("/health")
async def health():
    """Confirm the caches are writable — cheap post-deploy smoke check."""
    from gateway.platforms.base import get_document_cache_dir, get_image_cache_dir

    doc_dir = get_document_cache_dir()
    img_dir = get_image_cache_dir()
    return {
        "ok": True,
        "document_cache": str(doc_dir),
        "document_cache_writable": os.access(doc_dir, os.W_OK),
        "image_cache": str(img_dir),
        "image_cache_writable": os.access(img_dir, os.W_OK),
    }
