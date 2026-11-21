"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import Pool, RabbitDep, RedisDep
from app.core.errors import ORJSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(pool: Pool, redis: RedisDep, rabbit: RabbitDep) -> ORJSONResponse:
    checks: dict[str, str] = {}
    try:
        await pool.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    checks["rabbitmq"] = "ok" if rabbit.is_connected else "error: not connected"
    ready = all(v == "ok" for v in checks.values())
    return ORJSONResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks}, status_code=200 if ready else 503
    )
