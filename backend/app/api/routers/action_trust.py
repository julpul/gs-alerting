"""Trust ladder endpoints (spec 11.5): per (domain, action) level and cap."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentScope, CurrentUser, Pool
from app.core.audit import audit, user_actor
from app.core.errors import forbidden, not_found
from app.core.ids import parse_uuid
from app.core.scope import Scope
from app.runbooks import trust
from app.runbooks.actions.registry import get_action
from app.schemas import api as wire

router = APIRouter(prefix="/v1/action-trust", tags=["runbooks"])


class TrustBody(BaseModel):
    level: wire.TrustLevel | None = None
    cap: wire.TrustLevel | None = None


def _out(t: trust.Trust) -> dict[str, Any]:
    return {
        "domainId": str(t.domain_id),
        "action": t.action,
        "level": t.level,
        "cap": t.cap,
        "successfulApprovedRuns": t.successes,
        "failures": t.failures,
        "promoteAfter": trust.PROMOTE_AFTER,
        "lastChangedAt": t.changed_at,
    }


async def _domain_id(conn: asyncpg.Connection, ref: str) -> UUID | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchval("SELECT id FROM domains WHERE id = $1", uid)
    return await conn.fetchval("SELECT id FROM domains WHERE slug = $1", ref)


def _is_root(scope: Scope, domain_id: UUID) -> bool:
    return scope.is_master or any(
        r.role == "domain_root" and r.scope_type == "domain" and r.scope_id == domain_id for r in scope.roles
    )


@router.get("", response_model=list[wire.ActionTrust])
async def list_trust(scope: CurrentScope, pool: Pool, domainId: str | None = None) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        if domainId:
            domain_id = await _domain_id(conn, domainId)
            if domain_id is None or not scope.can_see_domain(domain_id):
                raise not_found("domain")
            domains = [domain_id]
        else:
            rows = await conn.fetch("SELECT id FROM domains ORDER BY name")
            domains = [r["id"] for r in rows if scope.can_see_domain(r["id"])]
        return [_out(t) for d in domains for t in await trust.list_trust(conn, d)]


@router.put("/{domain_id}/{action}", response_model=wire.ActionTrust)
async def set_trust(
    domain_id: str, action: str, body: TrustBody, scope: CurrentScope, user: CurrentUser, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        did = await _domain_id(conn, domain_id)
        if did is None or not scope.can_see_domain(did):
            raise not_found("domain")
        if get_action(action) is None:
            raise not_found("action")
        if not _is_root(scope, did):
            raise forbidden("only the Domain Root can change the trust level of an action")
        async with conn.transaction():
            changed = await trust.set_trust(
                conn,
                did,
                action,
                level=body.level.value if body.level else None,
                cap=body.cap.value if body.cap else None,
                cap_given="cap" in body.model_fields_set,
            )
            await audit(
                conn,
                user_actor(user.id, user.display_name),
                "action_trust.set",
                "domain",
                str(did),
                {"action": action, "level": changed.level, "cap": changed.cap},
            )
    return _out(changed)
