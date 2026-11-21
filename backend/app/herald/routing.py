"""Router factory for the Herald BFF: errors in the shape the UI reads (`{"detail": ...}`)."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.routing import APIRoute

from app.core.errors import AppError, ORJSONResponse


class HeraldRoute(APIRoute):
    """Our services raise `AppError(status, code, message)`; the Herald UI shows `detail`."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except AppError as exc:
                return ORJSONResponse({"detail": exc.message}, status_code=exc.status_code)
            except HTTPException:
                raise

        return wrapped


def herald_router(**kwargs: Any) -> APIRouter:
    """`router = herald_router()` in `app/api/routers/herald_*.py` (auto-discovered)."""
    return APIRouter(route_class=HeraldRoute, include_in_schema=False, **kwargs)
