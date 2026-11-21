"""Policy CRUD with scope resolution and permissions (spec 10.1).

Who may edit: Master the org defaults; Domain Root the policies of their domain and its teams; the rule's
team members (and the Domain Root / Master above them) the rule's policy. Anybody who can see the scope
may read it; a scope the caller cannot see is reported as not found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import audit, user_actor
from app.core.errors import AppError, forbidden, not_found
from app.core.ids import ORG_ID, parse_uuid
from app.core.scope import Scope
from app.escalation import refs
from app.schemas import api as wire
from app.schemas.internal import User

ORG_REFS = ("", "org", str(ORG_ID))


@dataclass(frozen=True)
class PolicyScopeInfo:
    scope_type: str
    scope_id: UUID
    team_id: UUID | None = None
    domain_ids: frozenset[UUID] = field(default_factory=frozenset)


async def resolve_scope(conn: asyncpg.Connection, scope_type: str, ref: str) -> PolicyScopeInfo:
    """`(scopeType, scopeId)` as sent on the wire -> ids and the teams / domains the scope belongs to."""
    if scope_type == "org":
        if ref not in ORG_REFS:
            raise not_found("org scope")
        return PolicyScopeInfo("org", ORG_ID)
    if scope_type == "domain":
        domain = await refs.domain_id(conn, ref)
        if domain is None:
            raise not_found("domain")
        return PolicyScopeInfo("domain", domain, domain_ids=frozenset({domain}))
    if scope_type == "team":
        team = await refs.team_id(conn, ref)
        if team is None:
            raise not_found("team")
        return PolicyScopeInfo("team", team, team, frozenset(await refs.team_domain_ids(conn, team)))
    rule_id = parse_uuid(ref)
    row = await conn.fetchrow("SELECT team_id FROM rules WHERE id = $1", rule_id) if rule_id else None
    if row is None:
        raise not_found("rule")
    team = row["team_id"]
    return PolicyScopeInfo("rule", rule_id, team, frozenset(await refs.team_domain_ids(conn, team)))


def can_view(scope: Scope, info: PolicyScopeInfo) -> bool:
    if info.scope_type == "org":
        return True
    if info.scope_type == "domain":
        return scope.can_see_domain(info.scope_id)
    return info.team_id is not None and scope.can_see_team(info.team_id)


def can_edit(scope: Scope, info: PolicyScopeInfo) -> bool:
    if scope.is_master:
        return True
    if info.scope_type == "org":
        return False
    domain_root = any(
        r.role == "domain_root" and r.scope_type == "domain" and r.scope_id in info.domain_ids
        for r in scope.roles
    )
    if domain_root:
        return True
    return info.scope_type == "rule" and any(
        r.role == "member" and r.scope_type == "team" and r.scope_id == info.team_id for r in scope.roles
    )


def require_view(scope: Scope, info: PolicyScopeInfo) -> None:
    if not can_view(scope, info):
        raise not_found(f"{info.scope_type} scope")


def require_edit(scope: Scope, info: PolicyScopeInfo) -> None:
    require_view(scope, info)
    if not can_edit(scope, info):
        raise forbidden(f"you may not edit escalation policies of this {info.scope_type}")


def validate_policy(policy: wire.EscalationPolicy) -> None:
    """Semantic checks beyond the schema (422 with the field path)."""
    for i, level in enumerate(policy.levels or []):
        path = f"levels[{i}]"
        if level.level != i + 1:
            raise AppError(422, "validation_error", f"{path}.level: levels must be numbered 1..n in order")
        if not level.channels:
            raise AppError(422, "validation_error", f"{path}.channels: at least one channel is required")
        if level.target == wire.Target.custom and not level.recipients:
            raise AppError(422, "validation_error", f"{path}.recipients: required for target=custom")
        if level.ackTimeoutSeconds is not None and level.ackTimeoutSeconds <= 0:
            raise AppError(422, "validation_error", f"{path}.ackTimeoutSeconds: must be positive or null")
    for name in ("renotifySeconds", "softDigestMinutes"):
        value = getattr(policy, name)
        if value is not None and value < 0:
            raise AppError(422, "validation_error", f"{name}: must not be negative")


def policy_out(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    return {
        "scopeType": row["scope_type"],
        "scopeId": str(row["scope_id"]),
        "severity": row["severity"],
        "levels": row["levels"] or [],
        "renotifySeconds": row["renotify_seconds"],
        "softChannels": list(row["soft_channels"] or []),
        "softDigestMinutes": row["soft_digest_minutes"],
    }


async def list_policies(conn: asyncpg.Connection, info: PolicyScopeInfo) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM escalation_policies WHERE scope_type = $1 AND scope_id = $2 "
        "ORDER BY array_position(ARRAY['critical','error','warning','info'], severity)",
        info.scope_type,
        info.scope_id,
    )
    return [policy_out(r) for r in rows]


async def upsert_policy(
    conn: asyncpg.Connection, info: PolicyScopeInfo, policy: wire.EscalationPolicy, user: User
) -> dict[str, Any]:
    validate_policy(policy)
    levels = [lv.model_dump(mode="json", exclude_none=True) for lv in policy.levels or []]
    row = await conn.fetchrow(
        "INSERT INTO escalation_policies (scope_type, scope_id, severity, levels, renotify_seconds, "
        "soft_channels, soft_digest_minutes, updated_by, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now()) "
        "ON CONFLICT (scope_type, scope_id, severity) DO UPDATE SET levels = EXCLUDED.levels, "
        "renotify_seconds = EXCLUDED.renotify_seconds, soft_channels = EXCLUDED.soft_channels, "
        "soft_digest_minutes = EXCLUDED.soft_digest_minutes, updated_by = EXCLUDED.updated_by, "
        "updated_at = now() RETURNING *",
        info.scope_type,
        info.scope_id,
        policy.severity.value,
        levels,
        policy.renotifySeconds,
        [c.value for c in policy.softChannels or []],
        policy.softDigestMinutes,
        user.id,
    )
    await audit(
        conn,
        user_actor(user.id, user.display_name),
        "escalation_policy.set",
        "escalation_policy",
        f"{info.scope_type}:{info.scope_id}:{policy.severity.value}",
    )
    return policy_out(row)


async def delete_policy(conn: asyncpg.Connection, info: PolicyScopeInfo, severity: str, user: User) -> bool:
    deleted = await conn.fetchval(
        "DELETE FROM escalation_policies WHERE scope_type = $1 AND scope_id = $2 AND severity = $3 "
        "RETURNING 1",
        info.scope_type,
        info.scope_id,
        severity,
    )
    if deleted:
        await audit(
            conn,
            user_actor(user.id, user.display_name),
            "escalation_policy.delete",
            "escalation_policy",
            f"{info.scope_type}:{info.scope_id}:{severity}",
        )
    return bool(deleted)
