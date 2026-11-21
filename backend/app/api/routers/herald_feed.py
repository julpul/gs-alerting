"""Herald UI feeds: delivery log (`/api/deliveries`), audit trail (`/api/audit`), numbers (`/api/stats`)."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Query

from app.api.deps import Pool, RedisDep
from app.core.errors import forbidden
from app.herald import queries
from app.herald.identity import Actor
from app.herald.ids import team_for_owner
from app.herald.routing import herald_router
from app.repositories.metrics import metrics_summary

log = logging.getLogger(__name__)
router = herald_router(prefix="/api", tags=["herald"])


@router.get("/deliveries")
async def deliveries(
    who: Actor, pool: Pool, owner: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 60
) -> list[dict[str, Any]]:
    """Newest notifications first, with attempts and event-received -> delivered latency."""
    async with pool.acquire() as conn:
        team_id = (
            await team_for_owner(conn, owner, who.scope) if owner and owner.startswith("team:") else None
        )
        return await queries.recent_deliveries(conn, who.scope, team_id, limit)


@router.get("/audit")
async def audit(who: Actor, pool: Pool, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
    """Approvers (Domain Roots) and admins (masters) only, like Herald. `intact` is always true: our audit
    log is append-only but not hash-chained; each entry's `hash` is a sha256 of the stored row."""
    if who.herald_role not in ("approver", "admin"):
        raise forbidden("the audit trail needs the approver role")
    async with pool.acquire() as conn:
        entries = await queries.audit_entries(conn, who.scope, limit)
    return {"intact": True, "chained": False, "entries": entries}


@router.get("/stats")
async def stats(
    who: Actor, pool: Pool, redis: RedisDep, since: float = 0.0, run: str | None = None
) -> dict[str, Any]:
    """Herald counters (`delivered`, `outstanding`, `p50_ms`, `p99_ms`, `max_ms`, `last_delivered`,
    `matched_events`) over our deliveries since `since` (epoch s), plus our `/v1/metrics/summary` numbers."""
    async with pool.acquire() as conn:
        out = await queries.delivery_stats(conn, who.scope, since, run)
    try:
        out["summary"] = await metrics_summary(pool, redis, who.scope)
    except Exception:
        log.warning("metrics summary unavailable for /api/stats", exc_info=True)
        out["summary"] = None
    return out
