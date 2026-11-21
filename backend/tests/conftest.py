"""Test infrastructure: per-session database + RabbitMQ/Redis prefixes so parallel suites never collide.

Requires `make infra-up` (Postgres, Redis, RabbitMQ). Env defaults match CI.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio

DEFAULT_ENV = {
    "DATABASE_URL": "postgresql://alerting:alerting@localhost:5432/alerting",
    "REDIS_URL": "redis://localhost:6379/0",
    "RABBITMQ_URL": "amqp://guest:guest@localhost:5672/",
    "SMTP_HOST": "localhost",
    "SMTP_PORT": "1025",
    "JWT_SECRET": "test-jwt-secret",
    "APP_SECRET": "test-app-secret",
    # The shared test database holds the seven original demo identities only (no history, no knowledge,
    # no embeddings); tests/seed/ seeds the full Vistula Capital profile into its own database.
    "SEED_PROFILE": "core",
    "SEED_SKIP_EMBEDDINGS": "1",
}
FORCED_ENV = {
    "AUTH_MODE": "demo",
    "DEMO_PASSWORD": "",
    "OIDC_ISSUER": "",
    "OIDC_CLIENT_SECRET": "",
    "JWT_SECRET": DEFAULT_ENV["JWT_SECRET"],
    "APP_SECRET": DEFAULT_ENV["APP_SECRET"],
}


def _with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


async def _delete_prefixed_rabbit(prefix: str) -> None:
    """Remove leftovers of this run's prefix via the management API (best effort)."""
    host = urlsplit(os.environ["RABBITMQ_URL"]).hostname or "localhost"
    auth = ("guest", "guest")
    async with httpx.AsyncClient(base_url=f"http://{host}:15672/api", auth=auth, timeout=10) as client:
        try:
            for kind in ("queues", "exchanges"):
                resp = await client.get(f"/{kind}/%2F")
                for item in resp.json():
                    if item["name"].startswith(prefix):
                        await client.delete(f"/{kind}/%2F/{item['name']}")
        except httpx.HTTPError:
            pass


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def session_env() -> AsyncIterator[dict[str, str]]:
    for key, value in DEFAULT_ENV.items():
        os.environ.setdefault(key, value)
    # Never the developer's sign-in setup: LiteLLM loads the repo `.env` on import (python-dotenv does not
    # override variables that are already set, so pinning them here wins).
    for key, value in FORCED_ENV.items():
        os.environ[key] = value
    token = uuid.uuid4().hex[:8]
    admin_dsn = os.environ["DATABASE_URL"]
    db_name = f"test_{token}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    os.environ["DATABASE_URL"] = _with_database(admin_dsn, db_name)
    os.environ["MQ_PREFIX"] = f"t{token}."
    os.environ["REDIS_PREFIX"] = f"t{token}:"
    os.environ["BCRYPT_ROUNDS"] = "4"
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        yield {
            "db": db_name,
            "mq_prefix": os.environ["MQ_PREFIX"],
            "redis_prefix": os.environ["REDIS_PREFIX"],
        }
    finally:
        from app.core.redis import close_redis, get_redis

        redis = get_redis()
        keys = [k async for k in redis.scan_iter(match=f"{os.environ['REDIS_PREFIX']}*")]
        if keys:
            await redis.delete(*keys)
        await close_redis()
        await _delete_prefixed_rabbit(os.environ["MQ_PREFIX"])
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        finally:
            await admin.close()
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def settings(session_env):
    from app.config import get_settings

    return get_settings()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pool(session_env) -> AsyncIterator[asyncpg.Pool]:
    from app.core.db import create_pool, run_migrations

    pool = await create_pool()
    await run_migrations(pool)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def redis(session_env):
    from app.core.redis import get_redis

    return get_redis()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def rabbit(session_env) -> AsyncIterator:
    from app.core.rabbit import Rabbit

    rabbit = Rabbit()
    await rabbit.connect()
    await rabbit.declare_topology()
    yield rabbit
    await rabbit.delete_topology()
    await rabbit.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def seeded(pool) -> AsyncIterator[asyncpg.Pool]:
    from seed.seed import run_seed

    await run_seed(pool)
    yield pool


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def app(seeded) -> AsyncIterator:
    """The FastAPI app with its lifespan running (own pool/redis/rabbit against the test database)."""
    from app.api.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def auth_headers(client) -> Callable[[str], Awaitable[dict[str, str]]]:
    """`await auth_headers("anna@demo")` -> {"Authorization": "Bearer ..."} (password `demo`)."""
    cache: dict[str, dict[str, str]] = {}

    async def _headers(email: str, password: str = "demo") -> dict[str, str]:
        if email not in cache:
            resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
            assert resp.status_code == 200, resp.text
            cache[email] = {"Authorization": f"Bearer {resp.json()['accessToken']}"}
        return cache[email]

    return _headers
