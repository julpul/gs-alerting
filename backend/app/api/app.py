"""FastAPI application factory. Routers in `app/api/routers/*.py` are auto-discovered."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.core import metrics  # noqa: F401  (registers the Prometheus metrics)
from app.core.db import create_pool, run_migrations
from app.core.errors import ORJSONResponse, install_error_handlers
from app.core.logging import setup_logging
from app.core.rabbit import Rabbit
from app.core.redis import close_redis, get_redis

log = logging.getLogger(__name__)

ROUTERS_PACKAGE = "app.api.routers"


def discover_routers(package_name: str = ROUTERS_PACKAGE) -> list[APIRouter]:
    """Import every module of the package and collect its module-level `router`."""
    package = importlib.import_module(package_name)
    routers: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{package_name}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await create_pool()
    if not settings.skip_migrations:
        await run_migrations(app.state.pool)
    app.state.redis = get_redis()
    app.state.rabbit = Rabbit()
    await app.state.rabbit.connect()
    await app.state.rabbit.declare_topology()
    log.info("api started", extra={"environment": settings.environment})
    try:
        yield
    finally:
        await app.state.rabbit.close()
        await close_redis()
        await app.state.pool.close()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title="Flexible Alerting & Notification Service",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    for router in discover_routers():
        app.include_router(router)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
