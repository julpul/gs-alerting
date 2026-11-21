"""GET /v1/metrics/summary: numbers for the demo metrics page."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentScope, Pool, RedisDep
from app.repositories import metrics as repo
from app.schemas import api as wire

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])


class Percentiles(BaseModel):
    p50: float | None = None
    p99: float | None = None
    count: int = 0


class MetricsSummaryOut(wire.MetricsSummary):
    windowMinutes: int
    outcomes: dict[str, int]
    queues: dict[str, int] | None = None
    processingLatencyMs: Percentiles | None = None
    deliveryLatencyMs: dict[str, Percentiles]


@router.get("/summary", response_model=MetricsSummaryOut)
async def summary(
    scope: CurrentScope,
    pool: Pool,
    redis: RedisDep,
    windowMinutes: Annotated[int, Query(ge=1, le=1440)] = 15,
) -> dict[str, Any]:
    return await repo.metrics_summary(pool, redis, scope, windowMinutes)
