"""SLA router (§3.2/§3.3): personal stats and the incident feed.

Operators observe their own incidents plus every still-open one; supervisors
observe the whole board. ``remaining_seconds`` and ``is_overdue`` are always
recomputed server-side from ``deadline_at`` (via the shared ``sla_snapshot_dict``)
so a stale row can never freeze the frontend countdown.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Operator, SLAIncident, SlaIncidentStatus
from app.routers._common import sla_snapshot
from app.routers.chats import is_supervisor
from app.routers.envelopes import SlaIncidentListResponse
from app.routers.shift import load_personal_stats
from app.schemas import PersonalSlaStats
from app.security import get_current_operator

logger = logging.getLogger("app.routers.sla")

router = APIRouter(prefix="/sla", tags=["sla"])

IncidentStatusFilter = Literal["PENDING", "RESOLVED"]


@router.get(
    "/stats/me",
    response_model=PersonalSlaStats,
    status_code=status.HTTP_200_OK,
    summary="Personal SLA statistics for the calling operator",
)
async def my_stats(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    """Return ``<PersonalSlaStats>`` computed by ``sla_engine.personal_stats``."""
    return await load_personal_stats(db, operator)


@router.get(
    "/incidents",
    response_model=SlaIncidentListResponse,
    status_code=status.HTTP_200_OK,
    summary="SLA incident feed (own incidents plus all still-open ones)",
)
async def list_incidents(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    incident_status: Annotated[
        IncidentStatusFilter | None,
        Query(alias="status", description="Filter by incident status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Maximum number of incidents")] = 100,
) -> SlaIncidentListResponse:
    """Return ``{"items": [SlaSnapshot, ...]}`` ordered by SLA urgency."""
    stmt = select(SLAIncident)

    if incident_status is not None:
        stmt = stmt.where(SLAIncident.status == SlaIncidentStatus(incident_status).value)
    else:
        # No filter: the operator's own incidents plus every still-open one.
        stmt = stmt.where(
            or_(
                SLAIncident.status == SlaIncidentStatus.PENDING.value,
                SLAIncident.assigned_operator_id == operator.id,
            )
        )

    if not is_supervisor(operator):
        stmt = stmt.where(
            or_(
                SLAIncident.assigned_operator_id == operator.id,
                SLAIncident.status == SlaIncidentStatus.PENDING.value,
            )
        )

    incidents = (
        await db.execute(
            stmt.order_by(
                SLAIncident.status.asc(),
                SLAIncident.deadline_at.asc(),
                SLAIncident.id.desc(),
            ).limit(limit)
        )
    ).scalars().all()

    logger.debug("incident feed for operator %s: %s rows", operator.id, len(incidents))
    # §3.3 shape: status / deadline_at / remaining_seconds / is_overdue, recomputed now.
    items = [sla_snapshot(incident) for incident in incidents]
    return SlaIncidentListResponse.model_validate({"items": items})
