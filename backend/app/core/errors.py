"""Error model `{code, message, details}` and its FastAPI handlers; ORJSON response class."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class ORJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_UTC_Z)


class AppError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str = "resource") -> AppError:
    return AppError(404, "not_found", f"{what} not found")


def forbidden(message: str = "forbidden") -> AppError:
    return AppError(403, "forbidden", message)


def unauthorized(message: str = "authentication required") -> AppError:
    return AppError(401, "unauthorized", message)


def _body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return body


def _field_path(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc)


async def _app_error(_: Request, exc: AppError) -> ORJSONResponse:
    return ORJSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status_code)


async def _http_error(_: Request, exc: StarletteHTTPException) -> ORJSONResponse:
    code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
    response = ORJSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code)
    if isinstance(exc, HTTPException) and exc.headers:
        response.headers.update(exc.headers)
    return response


async def _validation_error(_: Request, exc: RequestValidationError) -> ORJSONResponse:
    errors = [{"path": _field_path(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
    first = errors[0] if errors else {"path": "", "message": "invalid request"}
    return ORJSONResponse(
        _body("validation_error", f"{first['path']}: {first['message']}", {"errors": errors}),
        status_code=422,
    )


async def _unhandled(_: Request, exc: Exception) -> ORJSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return ORJSONResponse(_body("internal_error", "internal server error"), status_code=500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
