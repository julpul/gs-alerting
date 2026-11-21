import json
import logging
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.core.errors import AppError, ORJSONResponse, forbidden, install_error_handlers, not_found
from app.core.ids import ORG_ID, format_short_id, parse_short_id, parse_uuid, stable_id, uuid7
from app.core.logging import JsonFormatter
from app.core.redis import rkey


def test_uuid7_is_time_ordered_and_unique():
    a, b = uuid7(), uuid7()
    assert a != b and a.version == 7
    assert isinstance(a, UUID)


def test_stable_ids_are_deterministic():
    assert stable_id("user", "anna@demo") == stable_id("user", "anna@demo")
    assert stable_id("user", "x") != stable_id("team", "x")
    assert str(ORG_ID) == "00000000-0000-4000-8000-000000000001"


def test_short_ids():
    assert format_short_id(1234) == "A-1234"
    assert parse_short_id("A-1234") == 1234
    assert parse_short_id("a-7") == 7
    assert parse_short_id(str(uuid7())) is None
    assert parse_uuid("nope") is None
    assert parse_uuid(str(ORG_ID)) == ORG_ID


def test_rkey_applies_prefix(settings):
    assert rkey("evt", "1") == f"{settings.redis_prefix}evt:1"


def test_settings_defaults_and_cors_list():
    s = Settings(_env_file=None, cors_origins="http://a, http://b ,")
    assert s.cors_origin_list == ["http://a", "http://b"]


def test_json_log_format():
    record = logging.LogRecord("x", logging.WARNING, "f", 1, "hello %s", ("w",), None)
    record.alert_id = "A-1"
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hello w" and out["level"] == "WARNING" and out["alert_id"] == "A-1"


async def test_error_handlers_shapes():
    app = FastAPI(default_response_class=ORJSONResponse)
    install_error_handlers(app)

    @app.get("/nf")
    async def nf():
        raise not_found("alert")

    @app.get("/fb")
    async def fb():
        raise forbidden()

    @app.get("/custom")
    async def custom():
        raise AppError(409, "conflict", "already there", {"id": "x"})

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret detail")

    @app.get("/typed/{n}")
    async def typed(n: int):
        return {"n": n}

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        assert (await c.get("/nf")).json() == {"code": "not_found", "message": "alert not found"}
        assert (await c.get("/fb")).status_code == 403
        conflict = await c.get("/custom")
        assert conflict.status_code == 409 and conflict.json()["details"] == {"id": "x"}
        boom = await c.get("/boom")
        assert boom.status_code == 500 and boom.json()["code"] == "internal_error"
        assert "secret" not in boom.text
        bad = await c.get("/typed/abc")
        assert bad.status_code == 422 and bad.json()["details"]["errors"][0]["path"] == "path.n"
