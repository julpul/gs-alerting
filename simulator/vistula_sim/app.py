"""FastAPI app: control panel, stats and scenario endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import Settings
from .engine import Simulator
from .panel import PANEL_HTML
from .scenarios import SCENARIOS
from .transport import AmqpPublisher, HttpSender


class BaselineUpdate(BaseModel):
    rate: float | None = None
    paused: bool | None = None
    autoIncidents: bool | None = None


def build_simulator(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> Simulator:
    client = httpx.AsyncClient(
        base_url=settings.target_api_url, timeout=settings.request_timeout, transport=transport
    )
    sender = HttpSender(
        settings.target_api_url,
        client=client,
        max_retries=settings.max_retries,
        timeout=settings.request_timeout,
    )
    amqp = (
        AmqpPublisher(settings.customer_amqp_url, settings.trade_queue)
        if settings.customer_amqp_url
        else None
    )
    return Simulator(settings, sender, amqp)


def create_app(settings: Settings | None = None, simulator: Simulator | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    sim = simulator or build_simulator(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.baseline_enabled:
            sim.start_background()
        yield
        await sim.shutdown()

    app = FastAPI(title="Vistula Capital simulator", lifespan=lifespan)
    app.state.simulator = sim

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def panel() -> str:
        return PANEL_HTML

    @app.get("/stats")
    async def stats() -> dict:
        return sim.snapshot()

    @app.get("/scenarios")
    async def scenarios() -> list[dict[str, str]]:
        return [{"name": s.name, "description": s.description} for s in SCENARIOS.values()]

    @app.post("/scenario/{name}", status_code=202)
    async def run_scenario(name: str, request: Request, intensity: float = 1.0) -> dict:
        if name not in SCENARIOS:
            raise HTTPException(404, f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}")
        params = {k: v for k, v in request.query_params.items() if k != "intensity"}
        steps = sim.start_scenario(name, intensity, params)
        return {"scenario": name, "intensity": intensity, "steps": steps}

    @app.post("/baseline")
    async def baseline(update: BaselineUpdate) -> dict:
        if update.rate is not None:
            sim.base_rate = max(update.rate, 0.0)
        if update.paused is not None:
            sim.paused = update.paused
        if update.autoIncidents is not None:
            sim.auto_incidents = update.autoIncidents
        return sim.snapshot()["baseline"]

    return app
