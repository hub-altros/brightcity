"""JSON API: просмотр/скачивание медиафайлов + публичная отдача для ботов."""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from web_app.dependencies import (
    require_api_admin, require_admin, run_blocking,
)
from web_app.media import (
    get_media_bytes, get_media_row, verify_media_sig,
)

logger = logging.getLogger("web.api.media")

# Основной роутер: /api/media/*
router = APIRouter(prefix="/api/media", tags=["media"])

# Legacy-роутер без префикса: /media/* (фронт и боты используют эти URL)
legacy_router = APIRouter(tags=["media-public"])


# ─── MIME / extension helpers ─────────────────────────────────────────────

_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".avif": "image/avif",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}

_KIND_TO_MIME = {
    "photo": "image/jpeg",
    "banner": "image/jpeg",
    "doc": "application/pdf",
    "pdf": "application/pdf",
}


def _is_doc_kind(kind: str) -> bool:
    kind = (kind or "").lower()
    return (
        kind == "doc"
        or kind.startswith("doc")
        or "doc" in kind.split("_")
        or "pdf" in kind
    )


def _detect_mime(row: dict, data: bytes) -> str:
    for candidate in (row.get("url"), row.get("vk_url")):
        if not candidate or not isinstance(candidate, str):
            continue
        clean = candidate.split("?")[0].lower()
        for ext, mime in _MIME_BY_EXT.items():
            if clean.endswith(ext):
                return mime

    kind = (row.get("kind") or "").lower()
    if kind in _KIND_TO_MIME:
        return _KIND_TO_MIME[kind]
    if _is_doc_kind(kind):
        return "application/pdf"
    if "photo" in kind or "image" in kind:
        return "image/jpeg"

    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and b"WEBP" in head[:16]:
        return "image/webp"

    return "application/octet-stream"


def _filename_from_mime(media_id: int, mime: str) -> str:
    for ext, m in _MIME_BY_EXT.items():
        if m == mime:
            return f"media_{media_id}{ext}"
    return f"media_{media_id}"


def _build_inline_response(media_id: int, data: bytes, row: dict,
                           cache: str) -> Response:
    mime = _detect_mime(row, data)
    filename = _filename_from_mime(media_id, mime)
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
        "Content-Length": str(len(data)),
        "Cache-Control": cache,
    }
    return Response(content=data, media_type=mime, headers=headers)


# ─── /media/{id} и /media/raw/{id} (legacy_router) ──────────────────────

@legacy_router.get("/media/{media_id}")
async def media_get(media_id: int, admin=Depends(require_admin)):
    """Медиа для авторизованного админа (превью в React-карточке)."""
    data = await run_blocking(get_media_bytes, media_id)
    if not data:
        return Response(status_code=404, content=b"media not found")
    row = await run_blocking(get_media_row, media_id) or {}
    return _build_inline_response(media_id, data, row,
                                  cache="private, max-age=86400")


@legacy_router.get("/media/raw/{media_id}")
async def media_raw(media_id: int, sig: str = Query("")):
    """Публичная отдача для ботов (HMAC-подпись media_id)."""
    if not verify_media_sig(media_id, sig):
        logger.warning("media_raw: bad sig for id=%s", media_id)
        return Response(status_code=403, content=b"forbidden")
    data = await run_blocking(get_media_bytes, media_id)
    if not data:
        return Response(status_code=404, content=b"media not found")
    row = await run_blocking(get_media_row, media_id) or {}
    return _build_inline_response(media_id, data, row,
                                  cache="public, max-age=3600")


# ─── /api/media/{id}/download (router с префиксом) ──────────────────────

def _ext_for(row: dict, data: bytes | None) -> str:
    kind = (row.get("kind") or "").lower()
    if "pdf" in kind:
        return "pdf"
    if kind == "doc":
        return "docx"

    for candidate in (row.get("url"), row.get("vk_url")):
        if isinstance(candidate, str):
            clean = candidate.split("?")[0].lower()
            for ext in ("pdf", "docx", "doc", "png", "jpg", "jpeg", "webp"):
                if clean.endswith("." + ext):
                    return "jpg" if ext == "jpeg" else ext

    if data:
        if data[:4] == b"%PDF":
            return "pdf"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:2] == b"\xff\xd8":
            return "jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data[:4] == b"PK\x03\x04":
            return "docx"
        if data[:4] == b"\xd0\xcf\x11\xe0":
            return "doc"

    return "bin"


def _content_type_for(ext: str) -> str:
    return {
        "pdf":  "application/pdf",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "webp": "image/webp",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc":  "application/msword",
    }.get(ext, "application/octet-stream")


@router.get("/{media_id}/download")
async def media_download(
    media_id: int, admin=Depends(require_api_admin),
):
    row = await run_blocking(get_media_row, media_id)
    if not row:
        raise HTTPException(404, "media not found")
    data = await run_blocking(get_media_bytes, media_id)
    if not data:
        raise HTTPException(404, "media not found")

    ext = _ext_for(row, data)
    ctype = _content_type_for(ext)
    filename = f"media-{media_id}.{ext}"

    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )