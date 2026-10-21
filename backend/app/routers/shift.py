"""Shift router (§3.2): personal SLA card, roster, shift status, heartbeat.

``GET /shift/status`` is the ``PersonalSlaStats`` shortcut required by the
contract and delegates to ``sla_engine.personal_stats`` - the same source used by
``GET /sla/stats/me``. ``ustats_from_engine`` normalises that dict so a partial or
drifted payload can never produce an invalid response body.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Operator, ShiftStatus
from app.routers._common import as_contract, iso_or_none, operator_out, safe_broadcast_many
from app.routers.envelopes import OperatorListResponse
from app.schemas import HeartbeatOut, OperatorOut, PersonalSlaStats, ShiftStatusUpdate
from app.security import get_current_operator
from app.services import sla_engine
from app.utils import utcnow

logger = logging.getLogger("app.routers.shift")

router = APIRouter(prefix="/shift", tags=["shift"])


def _enum_str(value: Any) -> str:
    """Return the plain string of a (possibly) enum-typed value."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _as_payload(model: Any) -> dict[str, Any]:
    """Convert a pydantic model (or plain dict) into a JSON-ready dict."""
    if isinstance(model, dict):
        return dict(model)
    return dict(model.model_dump(mode="json"))


def ustats_from_engine(raw: Any, operator: Operator) -> Any:
    """Normalise ``sla_engine.personal_stats`` output into ``PersonalSlaStats``.

    The engine returns a complete contract dict; anything missing or ``None`` is
    repaired from the operator row so the frontend always receives the full §3.2
    shape (``sla_percent`` defaults to 100.0, never 0.0, for a fresh operator).
    """
    fallback: dict[str, Any] = {
        "operator_id": int(operator.id),
        "shift_status": _enum_str(operator.shift_status),
        "avg_response_seconds": 0,
        "resolved_count": 0,
        "open_count": 0,
        "overdue_count": 0,
        "sla_percent": 100.0,
        "sla_target_seconds": int(getattr(operator, "sla_target_seconds", 0) or 0),
    }
    if isinstance(raw, dict):
        incoming: dict[str, Any] = {key: value for key, value in raw.items() if value is not None}
    elif raw is not None and hasattr(raw, "model_dump"):
        incoming = {key: value for key, value in raw.model_dump().items() if value is not None}
    else:
        incoming = {}
    merged = {**fallback, **incoming}
    merged["operator_id"] = int(merged.get("operator_id") or operator.id)
    if not merged.get("shift_status"):
        merged["shift_status"] = _enum_str(operator.shift_status)
    return as_contract("PersonalSlaStats", merged)


async def load_personal_stats(db: AsyncSession, operator: Operator) -> Any:
    """Fetch and normalise the caller's ``PersonalSlaStats`` (``503`` on engine failure)."""
    try:
        raw = await sla_engine.personal_stats(db, operator)
    except Exception as exc:  # noqa: BLE001 - degrade instead of failing the poll
        logger.exception("personal_stats failed for operator %s", operator.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"SLA stats are temporarily unavailable ({type(exc).__name__})",
        ) from exc
    return ustats_from_engine(raw, operator)


@router.get(
    "/status",
    response_model=PersonalSlaStats,
    status_code=status.HTTP_200_OK,
    summary="Personal SLA stats for the calling operator",
)
async def shift_status(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    """Return ``<PersonalSlaStats>`` (same payload as ``GET /sla/stats/me``)."""
    return await load_personal_stats(db, operator)


@router.get(
    "/operators",
    response_model=OperatorListResponse,
    status_code=status.HTTP_200_OK,
    summary="Roster of every operator",
)
async def list_operators(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorListResponse:
    """Return ``{"items": [OperatorOut, ...]}`` for the sidebar roster."""
    rows = (await db.execute(select(Operator).order_by(Operator.id.asc()))).scalars().all()
    logger.debug("roster requested by operator %s: %s rows", operator.id, len(rows))
    return OperatorListResponse.model_validate({"items": [operator_out(row) for row in rows]})


@router.post(
    "/status",
    response_model=OperatorOut,
    status_code=status.HTTP_200_OK,
    summary="Change the calling operator's shift status",
    responses={422: {"description": "Unknown shift status"}},
)
async def update_shift_status(
    payload: ShiftStatusUpdate,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorOut:
    """Persist a new ``shift_status`` and announce ``SHIFT_CHANGED`` + ``OPERATOR_PRESENCE``."""
    raw_status = str(getattr(payload.status, "value", payload.status)).strip().upper()
    try:
        new_status = ShiftStatus(raw_status)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid shift status: {raw_status!r} (expected ONLINE, PAUSE or OFFLINE)",
        ) from exc

    operator.shift_status = new_status.value
    operator.updated_at = utcnow()
    await db.commit()
    await db.refresh(operator)

    payload_out = operator_out(operator)
    await safe_broadcast_many(
        [
            ("SHIFT_CHANGED", _as_payload(payload_out)),
            (
                "OPERATOR_PRESENCE",
                {
                    "operator_id": int(operator.id),
                    "is_online_ws": bool(operator.is_online_ws),
                    "shift_status": _enum_str(operator.shift_status),
                },
            ),
        ]
    )
    logger.info("operator %r switched shift status to %s", operator.username, _enum_str(new_status))
    return payload_out


@router.post(
    "/heartbeat",
    response_model=HeartbeatOut,
    status_code=status.HTTP_200_OK,
    summary="Liveness ping from the frontend (every 20 s)",
)
async def heartbeat(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HeartbeatOut:
    """Refresh ``last_seen_at`` / ``is_online_ws`` and return server time."""
    server_time = datetime.now(timezone.utc).replace(microsecond=0)
    operator.last_seen_at = server_time
    operator.is_online_ws = True
    operator.updated_at = server_time
    await db.commit()

    return HeartbeatOut.model_validate({"ok": True, "server_time": iso_or_none(server_time)})
