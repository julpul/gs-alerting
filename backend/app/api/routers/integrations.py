"""Integrations API: per-domain provider configuration; secrets are Fernet-encrypted and never returned."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from app.ai.llm import llm_configured
from app.api.deps import CurrentScope, CurrentUser, Pool
from app.core.audit import audit, user_actor
from app.core.errors import AppError, ORJSONResponse, forbidden
from app.core.ids import parse_uuid
from app.core.scope import Scope
from app.core.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])

KINDS = ("datadog", "github", "jira", "confluence", "llm")
LLM_MODES = ("default", "own_key", "own_endpoint")


class IntegrationBody(BaseModel):
    scopeDomainId: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


def _wire(kind: str, status: str, domain_id: UUID | None, mode: str | None) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": status,
        "scopeDomainId": str(domain_id) if domain_id else None,
        "mode": mode,
    }


def _default_row(kind: str) -> dict[str, Any]:
    if kind == "llm":
        return _wire(kind, "connected" if llm_configured() else "not_connected", None, "default")
    return _wire(kind, "not_connected", None, None)


@router.get("")
async def list_integrations(pool: Pool, scope: CurrentScope) -> ORJSONResponse:
    async with pool.acquire() as conn:
        if scope.is_master:
            rows = await conn.fetch("SELECT domain_id, kind, mode FROM integrations ORDER BY kind, domain_id")
        else:
            rows = await conn.fetch(
                "SELECT domain_id, kind, mode FROM integrations WHERE domain_id = ANY($1) "
                "ORDER BY kind, domain_id",
                list(scope.domain_ids),
            )
    out = [_wire(r["kind"], "connected", r["domain_id"], r["mode"]) for r in rows]
    present = {r["kind"] for r in rows}
    out += [_default_row(k) for k in KINDS if k not in present]
    return ORJSONResponse(out)


async def _domain(conn: asyncpg.Connection, scope: Scope, ref: str | None) -> UUID:
    """The domain a caller configures: given, or their only domain-root domain; they must be root/master."""
    roots = [r.scope_id for r in scope.roles if r.role == "domain_root" and r.scope_type == "domain"]
    if ref:
        domain_id = await conn.fetchval(
            "SELECT id FROM domains WHERE id = $1 OR slug = $2", parse_uuid(ref) or UUID(int=0), ref
        )
        if domain_id is None:
            raise AppError(422, "invalid_domain", "unknown domain")
    elif len(roots) == 1:
        domain_id = roots[0]
    else:
        raise AppError(422, "invalid_domain", "scopeDomainId is required")
    if not (scope.is_master or domain_id in roots):
        raise forbidden("only a domain root can configure integrations")
    return domain_id


async def _stored(conn: asyncpg.Connection, domain_id: UUID, kind: str) -> dict[str, Any]:
    blob = await conn.fetchval(
        "SELECT config_encrypted FROM integrations WHERE domain_id = $1 AND kind = $2", domain_id, kind
    )
    if not blob:
        return {}
    try:
        return json.loads(decrypt_secret(blob))
    except Exception:
        return {}


def _check_llm(config: dict[str, Any]) -> str:
    mode = str(config.get("mode") or "")
    if mode not in LLM_MODES:
        raise AppError(422, "invalid_config", f"mode must be one of {', '.join(LLM_MODES)}")
    if mode == "default":
        return mode
    if not config.get("model"):
        raise AppError(422, "invalid_config", "model is required")
    if mode == "own_key" and not config.get("apiKey"):
        raise AppError(422, "invalid_config", "apiKey is required for own_key")
    if mode == "own_endpoint":
        base = urlsplit(str(config.get("baseUrl") or ""))
        if base.scheme not in ("http", "https") or not base.hostname:
            raise AppError(422, "invalid_config", "baseUrl must be an http(s) URL for own_endpoint")
    return mode


@router.put("/{kind}")
async def put_integration(
    body: IntegrationBody,
    pool: Pool,
    user: CurrentUser,
    scope: CurrentScope,
    kind: str = Path(...),
) -> ORJSONResponse:
    if kind not in KINDS:
        raise AppError(422, "invalid_kind", f"kind must be one of {', '.join(KINDS)}")
    async with pool.acquire() as conn:
        domain_id = await _domain(conn, scope, body.scopeDomainId)
        existing = await _stored(conn, domain_id, kind)
        merged = {**existing, **{k: v for k, v in body.config.items() if v not in (None, "")}}
        mode: str | None = None
        if kind == "llm":
            mode = _check_llm(merged)
        elif not body.config:
            raise AppError(422, "invalid_config", "config is required")
        actor = user_actor(user.id, user.display_name)
        async with conn.transaction():
            if mode == "default":
                await conn.execute(
                    "DELETE FROM integrations WHERE domain_id = $1 AND kind = $2", domain_id, kind
                )
            else:
                await conn.execute(
                    "INSERT INTO integrations (domain_id, kind, config_encrypted, mode, updated_at) "
                    "VALUES ($1, $2, $3, $4, now()) ON CONFLICT (domain_id, kind) DO UPDATE "
                    "SET config_encrypted = EXCLUDED.config_encrypted, mode = EXCLUDED.mode, "
                    "updated_at = now()",
                    domain_id,
                    kind,
                    encrypt_secret(json.dumps(merged)),
                    mode,
                )
            await audit(
                conn,
                actor,
                "integration.update",
                "integration",
                f"{domain_id}:{kind}",
                {"kind": kind, "mode": mode, "fields": sorted(body.config)},
            )
    status = "not_connected" if mode == "default" and not llm_configured() else "connected"
    return ORJSONResponse(_wire(kind, status, domain_id, mode))
