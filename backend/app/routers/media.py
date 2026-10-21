"""Media router (§3.1): read-only access to stored attachments.

=============================================  ==================================
method + path                                   behaviour
=============================================  ==================================
``GET /api/messages/{message_id}/attachments``  ``200 AttachmentListOut`` / ``404``
``GET /api/media/{attachment_id}``              binary stream, ``Range`` support
``GET /api/media/{attachment_id}?variant=thumb``  the ``320 px`` JPEG preview
=============================================  ==================================

``POST /api/chats/{chat_id}/attachments`` deliberately lives in the messages
router: it creates a message *and* its attachment, so the transaction belongs
there. This module only reads.

Authentication — header first, query parameter as a fallback
-------------------------------------------------------------
Every route requires a valid access token. ``<img src="...">``, ``<audio>`` and
``<video>`` cannot send an ``Authorization`` header, so ``GET /api/media/{id}``
also accepts ``?token=<access_token>``. The header always wins when both are
present, so a normal ``fetch`` with a bearer header keeps working unchanged:

1. ``Authorization: Bearer <jwt>`` — the standard path (``app.security.bearer``);
2. ``?token=<jwt>`` — only consulted when the header is absent or does not carry
   a usable bearer credential.

Both paths run through the very same ``get_current_operator`` pipeline, so an
unknown, revoked or expired token is rejected identically (``401``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import GroupMessage, MediaAttachment, Operator
from app.routers._common import not_found
from app.schemas import AttachmentListOut
from app.security import bearer, get_current_operator, is_active_operator
from app.services import media as media_service

logger = logging.getLogger("tgdesk.media")

router = APIRouter(tags=["media"])

#: Query values that select the generated preview instead of the original file.
THUMBNAIL_VARIANTS: frozenset[str] = frozenset({"thumb", "thumbnail"})

#: Caching policy for attachment bytes: private, must be revalidated, never stored
#: by a shared proxy (the URL can carry the bearer token in ``?token=``).
MEDIA_CACHE_CONTROL: str = "private, max-age=0, must-revalidate"


# ---------------------------------------------------------------------------
# Authentication: header first, ``?token=`` fallback
# ---------------------------------------------------------------------------


async def current_operator_for_media(
    request: Request,
    token: Annotated[str | None, Query(description="Access token (fallback для <img src>)")] = None,
    db: AsyncSession = Depends(get_db),
) -> Operator:
    """Resolve the operator from the ``Authorization`` header, else from ``?token=``.

    ``<img src>`` cannot attach request headers, so media reads also accept the token as
    a query parameter. The header always wins when it is present and valid; otherwise the
    query token is validated by exactly the same code path, so both sources share one
    policy (expiry, revocation, inactive operator).
    """
    from fastapi.security import HTTPAuthorizationCredentials  # noqa: PLC0415 - local import

    raw_credential: str | None = None

    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        candidate = value.strip() if scheme.lower() == "bearer" else header.strip()
        if candidate:
            raw_credential = candidate

    if raw_credential is None:
        raw_credential = (token or "").strip()
        if raw_credential.lower().startswith("bearer "):
            raw_credential = raw_credential[7:].strip()

    if not raw_credential:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован: требуется access-токен",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_credential)
    resolved: Operator = await get_current_operator(credentials=credentials, db=db)
    # ``get_current_operator`` only proves the token is ours and its row exists. A
    # deactivated account must lose access to stored client files at the same moment
    # it loses the desk (``security.require_admin`` re-checks this on every call too).
    if not is_active_operator(resolved):
        logger.info(
            "media access refused for deactivated operator id=%s", getattr(resolved, "id", "?")
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Учётная запись деактивирована",
        )
    return resolved


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def _ensure_message_exists(db: AsyncSession, message_id: int) -> None:
    """Raise ``404`` when ``message_id`` is unknown (avoids an empty 200)."""
    exists = (
        await db.execute(select(GroupMessage.id).where(GroupMessage.id == int(message_id)).limit(1))
    ).scalar_one_or_none()
    if exists is None:
        raise not_found(f"Сообщение {message_id} не найдено")


async def get_attachment(db: AsyncSession, attachment_id: int) -> MediaAttachment:
    """Load one attachment row or raise ``404`` (shared by both routes)."""
    attachment = (
        await db.execute(
            select(MediaAttachment).where(MediaAttachment.id == int(attachment_id)).limit(1)
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise not_found(f"Вложение {attachment_id} не найдено")
    return attachment


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/messages/{message_id}/attachments",
    response_model=AttachmentListOut,
    summary="List the media attachments of a message",
    responses={
        401: {"description": "Missing or invalid access token"},
        404: {"description": "Unknown message"},
    },
)
async def list_message_attachments(
    message_id: int,
    operator: Annotated[Operator, Depends(current_operator_for_media)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AttachmentListOut:
    """Return ``{items: AttachmentOut[]}`` in insertion order (§3.1)."""
    await _ensure_message_exists(db, message_id)
    rows = await media_service.list_attachments(db, message_id)
    logger.debug(
        "оператор %s запросил вложения сообщения %s (%d шт.)",
        getattr(operator, "username", "?"),
        message_id,
        len(rows),
    )
    return AttachmentListOut(items=[media_service.attachment_to_out(row) for row in rows])


@router.get(
    "/media/{attachment_id}",
    response_class=FileResponse,
    summary="Download an attachment (or its thumbnail) with Range support",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "Full file or 206 partial"},
        206: {"description": "Partial content (Range)"},
        401: {"description": "Missing or invalid access token"},
        404: {"description": "Unknown attachment or missing file on disk"},
    },
)
async def download_attachment(
    attachment_id: int,
    operator: Annotated[Operator, Depends(current_operator_for_media)],
    db: Annotated[AsyncSession, Depends(get_db)],
    variant: Annotated[str | None, Query(description="'thumb' для миниатюры")] = None,
) -> FileResponse:
    """Stream the stored file.

    ``Range`` is handled entirely by Starlette's :class:`FileResponse`: it
    advertises ``Accept-Ranges: bytes`` and answers a ``Range`` request with
    ``206 Partial Content`` (single and multipart), which is what makes
    ``<video>``/``<audio>`` seeking work. A ``?variant=thumb`` request serves the
    ``320 px`` JPEG preview instead of the original.
    """
    attachment = await get_attachment(db, attachment_id)
    normalised_variant = (variant or "").strip().lower() or None
    if normalised_variant in THUMBNAIL_VARIANTS:
        normalised_variant = "thumb"
    elif normalised_variant == "original":
        normalised_variant = None

    try:
        response = media_service.attachment_stream(attachment, variant=normalised_variant)
    except ValueError as exc:
        if normalised_variant == "thumb" and not getattr(attachment, "thumbnail_path", None):
            raise not_found(f"Миниатюра вложения {attachment_id} не создана") from exc
        if normalised_variant == "thumb":
            raise not_found(f"Миниатюра вложения {attachment_id} отсутствует на диске") from exc
        logger.warning("вложение %s недоступно на диске: %s", attachment_id, exc)
        raise not_found(f"Файл вложения {attachment_id} отсутствует на диске") from exc

    logger.debug(
        "оператор %s скачал вложение %s (variant=%s, mime=%s)",
        getattr(operator, "username", "?"),
        attachment_id,
        normalised_variant,
        attachment.mime_type,
    )
    # A media read is private client data, and the URL may carry the access token in
    # its query string (``?token=``), so no shared cache and no referrer may keep it.
    response.headers["Cache-Control"] = MEDIA_CACHE_CONTROL
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


__all__ = [
    "MEDIA_CACHE_CONTROL",
    "THUMBNAIL_VARIANTS",
    "current_operator_for_media",
    "download_attachment",
    "get_attachment",
    "list_message_attachments",
    "router",
]
