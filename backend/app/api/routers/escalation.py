"""Escalation policies with inheritance (spec 10.1): CRUD per scope and the effective policy."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Response

from app.api.deps import CurrentScope, CurrentUser, Pool
from app.core.errors import AppError, not_found
from app.core.ids import ORG_ID
from app.escalation import policy_admin as admin
from app.escalation import refs
from app.escalation.policies import effective_policy
from app.repositories.alert_actions import _load_rule as load_rule
from app.schemas import api as wire

router = APIRouter(prefix="/v1/escalation-policies", tags=["schedules"])


@router.get("", response_model=list[wire.EscalationPolicy])
async def list_policies(
    scopeType: wire.PolicyScope, scopeId: str, scope: CurrentScope, pool: Pool
) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        info = await admin.resolve_scope(conn, scopeType.value, scopeId)
        admin.require_view(scope, info)
        return await admin.list_policies(conn, info)


@router.put("", response_model=wire.EscalationPolicy)
async def put_policy(
    body: wire.EscalationPolicy, user: CurrentUser, scope: CurrentScope, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        info = await admin.resolve_scope(conn, body.scopeType.value, body.scopeId)
        admin.require_edit(scope, info)
        return await admin.upsert_policy(conn, info, body, user)


@router.delete("", status_code=204)
async def delete_policy(
    scopeType: wire.PolicyScope,
    scopeId: str,
    severity: wire.Severity,
    user: CurrentUser,
    scope: CurrentScope,
    pool: Pool,
) -> Response:
    async with pool.acquire() as conn, conn.transaction():
        info = await admin.resolve_scope(conn, scopeType.value, scopeId)
        admin.require_edit(scope, info)
        await admin.delete_policy(conn, info, severity.value, user)
    return Response(status_code=204)


async def _domain_for(conn: asyncpg.Connection, team: UUID, ref: str | None) -> UUID:
    domains = await refs.team_domain_ids(conn, team)
    if ref:
        found = await refs.domain_id(conn, ref)
        if found is None or found not in domains:
            raise not_found("domain of this team")
        return found
    return domains[0] if domains else ORG_ID


@router.get("/effective", response_model=wire.EffectiveEscalationPolicy)
async def effective(
    severity: wire.Severity,
    scope: CurrentScope,
    pool: Pool,
    ruleId: str | None = None,
    teamId: str | None = None,
    domainId: str | None = None,
) -> wire.EffectiveEscalationPolicy:
    """Policy after rule -> team -> domain -> org inheritance. `domainId` picks one domain of a team that
    belongs to several (default: the first by slug)."""
    if not ruleId and not teamId:
        raise AppError(422, "validation_error", "teamId or ruleId is required")
    async with pool.acquire() as conn:
        rule: Any = None
        if ruleId:
            info = await admin.resolve_scope(conn, "rule", ruleId)
            rule = await load_rule(conn, {"rule_id": info.scope_id}) or {"id": info.scope_id}
        else:
            info = await admin.resolve_scope(conn, "team", teamId or "")
        admin.require_view(scope, info)
        team = info.team_id or info.scope_id
        policy = await effective_policy(
            conn,
            rule=rule,
            team_id=team,
            domain_id=await _domain_for(conn, team, domainId),
            severity=severity.value,
        )
    return policy.to_wire(info.scope_type, str(info.scope_id))
