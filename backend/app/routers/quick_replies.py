"""Quick replies router (CONTRACTS_V5 §5.2).

==============================================  ====================================
``GET    /api/quick-replies``                   live replies, ordered — any operator
``GET    /api/quick-replies?all=true``          including retired ones — **admin**
``POST   /api/quick-replies``                   create — **admin** -> ``201``
``PATCH  /api/quick-replies/{reply_id}``        partial update — **admin** -> ``200``
``DELETE /api/quick-replies/{reply_id}``        delete — **admin** -> ``204``
==============================================  ====================================

The list is editable at runtime (§5.4 removed the hard-coded constant from
``MessageInput.tsx``), so every operator must be able to **read** it while only an
administrator may change it. The whole list including retired replies is an
administrative view, so ``?all=true`` is administrator-only as well.

Authentication
--------------
``GET`` (without ``all``) reuses :func:`app.security.get_current_operator` — the
same dependency as ``app.routers.messages`` / ``app.routers.reactions``, i.e. the
ordinary desk session. Every mutation goes through
:func:`require_quick_reply_admin`, declared here.

Why a local dependency instead of :func:`app.security.require_admin`
--------------------------------------------------------------------
``app.security.require_admin`` accepts **only** an admin-scope token (minted by
``POST /api/admin/login``) and rejects the desk session the quick-reply editor is
used from; routing plain ``PATCH``es through it would force every administrator to
log into the admin panel first. It also cannot simply be stacked on top of
``get_current_operator``: both declare the same ``bearer``/``db`` dependencies
under different parameter names, so FastAPI would solve them twice and a desk
token would still fail the admin half.

:func:`require_quick_reply_admin` therefore accepts either credential shape and
is at least as strict as ``require_admin``:

* a desk (operator-scope) token whose operator is an active ``ADMIN``;
* **or** an admin-scope token from ``/api/admin/login`` whose operator is an
  active ``ADMIN`` (``decode_admin_token`` also admits a SUPERVISOR claim, so the
  role is re-checked here).

A supervisor, a plain operator, or a token belonging to a demoted/deactivated
account all receive ``403 {"detail": app.security.ADMIN_REQUIRED_DETAIL}`` — the
same message :func:`app.security.require_admin` emits.

Error contract (§5.2):

* ``404`` — no such reply (including a repeated ``DELETE``);
* ``409`` — ``shortcut`` already taken;
* ``422`` — blank ``body`` / ``title``, a ``shortcut`` outside
  ``^[A-Za-z0-9_-]{1,32}$``, or an empty ``PATCH``;
* ``401`` / ``403`` — missing token / not an administrator.

Every successful mutation makes the service broadcast ``QUICK_REPLIES_UPDATED``
(§5.3), so all connected operators refresh their composer without a reload. The
module does not mount itself: ``app.main.ROUTER_MOUNTS`` registers it under
``/api``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.enums import OperatorRole
from app.models import Operator
from app.schemas import QuickReplyIn, QuickReplyListOut, QuickReplyOut, QuickReplyUpdate
from app.security import (
    ADMIN_REQUIRED_DETAIL,
    InvalidTokenError,
    bearer,
    decode_access_token,
    decode_admin_token,
    get_current_operator,
)
from app.services.quick_replies import (
    QuickReplyConflictError,
    QuickReplyNotFoundError,
    QuickReplyValidationError,
    create_quick_reply,
    delete_quick_reply,
    list_quick_replies,
    update_quick_reply,
)

logger = logging.getLogger("tgdesk.routers.quick_replies")

router = APIRouter(tags=["quick-replies"])

__all__ = ["is_administrator", "require_quick_reply_admin", "router"]

#: Documented error bodies for the OpenAPI schema (§5.2).
_FORBIDDEN: Final[dict[str, str]] = {"description": ADMIN_REQUIRED_DETAIL}
_LIST_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Требуется токен"},
    403: _FORBIDDEN,
}
_WRITE_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Требуется токен"},
    403: _FORBIDDEN,
    409: {"description": "Быстрый ответ с таким shortcut уже существует"},
    422: {"description": "Недопустимое значение поля"},
}
_NOT_FOUND_OR_CONFLICT: dict[int | str, dict[str, Any]] = {
    401: {"description": "Требуется токен"},
    403: _FORBIDDEN,
    404: {"description": "Быстрый ответ не найден"},
    409: {"description": "Быстрый ответ с таким shortcut уже существует"},
    422: {"description": "Недопустимое значение поля"},
}
_DELETE_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Требуется токен"},
    403: _FORBIDDEN,
    404: {"description": "Быстрый ответ не найден"},
}

#: Shared dependency aliases — identical to ``routers/messages.py``.
CurrentOperator = Annotated[Operator, Depends(get_current_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


def _forbidden() -> HTTPException:
    """The ``403`` of the admin panel, byte-identical to ``app.security``'s."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ADMIN_REQUIRED_DETAIL)


def _unauthenticated() -> HTTPException:
    """``401`` for an absent or unusable bearer token."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется действительный токен",
        headers={"WWW-Authenticate": "Bearer"},
    )


def is_administrator(operator: Any) -> bool:
    """``True`` when ``operator`` is an **active ADMIN** (CONTRACTS_V2 §3.3).

    Shared by :func:`require_quick_reply_admin` and the ``?all=true`` branch of
    :func:`get_quick_replies`, so both apply exactly the same rule.
    """
    if operator is None:
        return False
    if not bool(getattr(operator, "is_active", True)):
        return False
    if not bool(getattr(operator, "is_admin", False)):
        return False
    return str(getattr(operator, "role", "") or "") == OperatorRole.ADMIN.value


async def require_quick_reply_admin(
    credentials: Annotated[Any, Depends(bearer)],
    db: DatabaseSession,
) -> Operator:
    """Allow only an active administrator — via either the desk or the admin login.

    See the module docstring for why this exists next to
    :func:`app.security.require_admin`. Identical checks, applied to whichever
    token arrives:

    1. the token must be present, well-formed and unexpired (``401`` otherwise);
    2. it must decode under **one** of the two validators — desk
       (``decode_access_token``) or admin panel (``decode_admin_token``);
    3. the referenced operator must exist and be active (``403``);
    4. its role must be ``ADMIN`` (``403``) — a supervisor claim inside an
       admin-scope token is therefore still refused.
    """
    if credentials is None or not getattr(credentials, "credentials", None):
        raise _unauthenticated()
    if str(getattr(credentials, "scheme", "") or "").lower() != "bearer":
        raise _unauthenticated()

    token = str(credentials.credentials)
    claims: Any = None
    for decode in (decode_access_token, decode_admin_token):
        try:
            claims = decode(token)
            break
        except InvalidTokenError:
            continue
    if claims is None:
        logger.info("quick-reply mutation refused: token is not a valid desk/admin token")
        raise _unauthenticated()

    operator_id = getattr(claims, "id", None)
    if operator_id is None:
        raise _forbidden()
    try:
        key = int(operator_id)
    except (TypeError, ValueError):
        raise _forbidden() from None

    operator = await db.get(Operator, key)
    if operator is None:
        logger.info("quick-reply mutation refused: token references a missing operator id=%s", key)
        raise _forbidden()
    if not is_administrator(operator):
        logger.info("quick-reply mutation refused: operator id=%s is not an active admin", key)
        raise _forbidden()
    return operator


QuickReplyAdmin = Annotated[Operator, Depends(require_quick_reply_admin)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found(reply_id: Any) -> HTTPException:
    """Uniform ``404`` for an unknown reply."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Быстрый ответ {reply_id} не найден",
    )


def _conflict(detail: str) -> HTTPException:
    """Uniform ``409`` for a duplicate ``shortcut``."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    """Uniform ``422`` for a rejected field value."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _out(reply: Any) -> QuickReplyOut:
    """Project a ``QuickReply`` row onto the §5.2 response shape."""
    return QuickReplyOut.from_orm_reply(reply)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/quick-replies",
    response_model=QuickReplyListOut,
    status_code=status.HTTP_200_OK,
    summary="Список быстрых ответов",
    responses=_LIST_ERRORS,
)
async def get_quick_replies(
    operator: CurrentOperator,
    db: DatabaseSession,
    include_all: Annotated[
        bool,
        Query(
            alias="all",
            description="Включить неактивные быстрые ответы (только администратор)",
        ),
    ] = False,
) -> QuickReplyListOut:
    """Return the quick-reply catalogue ordered by ``(sort_order, id)``.

    A plain ``GET`` returns the live replies every operator sees in the composer.
    ``?all=true`` additionally returns retired ones (``is_active=false``) so an
    administrator can reactivate or audit them; because that view exposes
    administrative state, it is opened only to an administrator — checked against
    the operator :func:`get_current_operator` already resolved, so no second
    token lookup happens.
    """
    include_inactive = bool(include_all)
    if include_inactive and not is_administrator(operator):
        logger.info(
            "quick-reply ?all=true refused: operator id=%s is not an active admin",
            getattr(operator, "id", "?"),
        )
        raise _forbidden()

    replies = await list_quick_replies(db, include_inactive=include_inactive)
    logger.debug(
        "operator %s read %d quick repl(y/ies) (include_inactive=%s)",
        operator.id,
        len(replies),
        include_inactive,
    )
    return QuickReplyListOut.from_orm_replies(replies)


@router.post(
    "/quick-replies",
    response_model=QuickReplyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать быстрый ответ",
    responses=_WRITE_ERRORS,
)
async def post_quick_reply(
    payload: QuickReplyIn,
    operator: QuickReplyAdmin,
    db: DatabaseSession,
) -> QuickReplyOut:
    """Create a quick reply authored by the calling administrator.

    ``201`` with the stored row; ``409`` when ``shortcut`` is already used (the
    unique index of §5.1 is the authority); ``422`` for a blank ``body`` or an
    unsafe ``shortcut``. All operators receive ``QUICK_REPLIES_UPDATED``.
    """
    try:
        reply = await create_quick_reply(db, payload=payload, operator=operator)
    except QuickReplyConflictError as exc:
        raise _conflict(str(exc)) from exc
    except QuickReplyValidationError as exc:
        raise _unprocessable(str(exc)) from exc

    logger.info("operator %s created quick reply %s", operator.id, getattr(reply, "id", "?"))
    return _out(reply)


@router.patch(
    "/quick-replies/{reply_id}",
    response_model=QuickReplyOut,
    status_code=status.HTTP_200_OK,
    summary="Изменить быстрый ответ",
    responses=_NOT_FOUND_OR_CONFLICT,
)
async def patch_quick_reply(
    reply_id: int,
    payload: QuickReplyUpdate,
    operator: QuickReplyAdmin,
    db: DatabaseSession,
) -> QuickReplyOut:
    """Partially update a quick reply: only the fields in the body are applied.

    ``404`` for an unknown id, ``409`` when the new ``shortcut`` belongs to
    another reply, ``422`` for an empty body or a rejected value. The author
    (``created_by``) is preserved. All operators receive
    ``QUICK_REPLIES_UPDATED``.
    """
    try:
        reply = await update_quick_reply(
            db, reply_id=reply_id, payload=payload, operator=operator
        )
    except QuickReplyNotFoundError as exc:
        raise _not_found(reply_id) from exc
    except QuickReplyConflictError as exc:
        raise _conflict(str(exc)) from exc
    except QuickReplyValidationError as exc:
        raise _unprocessable(str(exc)) from exc

    logger.info("operator %s updated quick reply %s", operator.id, reply_id)
    return _out(reply)


@router.delete(
    "/quick-replies/{reply_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Удалить быстрый ответ",
    responses=_DELETE_ERRORS,
)
async def delete_quick_reply_route(
    reply_id: int,
    operator: QuickReplyAdmin,
    db: DatabaseSession,
) -> Response:
    """Delete a quick reply.

    Answers ``204`` with an empty body; a repeated ``DELETE`` is a ``404``
    (§5.2). To retire a reply without losing it, ``PATCH {"is_active": false}``
    is the soft alternative. All operators receive ``QUICK_REPLIES_UPDATED``.
    """
    try:
        await delete_quick_reply(db, reply_id=reply_id, operator=operator)
    except QuickReplyNotFoundError as exc:
        raise _not_found(reply_id) from exc
    except QuickReplyValidationError as exc:
        raise _unprocessable(str(exc)) from exc

    logger.info("operator %s deleted quick reply %s", operator.id, reply_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
