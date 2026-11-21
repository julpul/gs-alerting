"""MCP tests: the real app served by uvicorn on a free port, the official SDK client over streamable HTTP."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
import pytest_asyncio
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.ai.llm import set_llm
from app.core.ids import stable_id
from app.core.rabbit import Rabbit
from app.core.security import generate_pat
from app.pipeline.rules.index import RuleIndex
from app.pipeline.rules.templates import render
from app.pipeline.worker import Worker
from tests.fakes.llm import FakeLLM
from tests.pipeline.conftest import purge_events_queue


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def server_url(app) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", timeout_graceful_shutdown=1
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def make_pat(client, auth_headers) -> Callable[..., Awaitable[SimpleNamespace]]:
    """`await make_pat("anna@demo", "claude-code")` -> token (plain), id, name."""

    async def factory(email: str, name: str = "test-agent") -> SimpleNamespace:
        resp = await client.post("/v1/tokens", json={"name": name}, headers=await auth_headers(email))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return SimpleNamespace(token=body["token"], id=body["id"], name=name, email=email)

    return factory


@pytest_asyncio.fixture(loop_scope="session")
async def informed_pat(seeded) -> AsyncIterator[Callable[..., Awaitable[SimpleNamespace]]]:
    """A fresh user with only the `informed` role on a team, plus a PAT for it (inserted with SQL)."""

    created: list[uuid.UUID] = []

    async def factory(team_slug: str, name: str = "informed-agent") -> SimpleNamespace:
        user_id = uuid.uuid4()
        created.append(user_id)
        token, _, token_hash = generate_pat()
        await seeded.execute(
            "INSERT INTO users (id, email, display_name, password_hash) VALUES ($1, $2, $3, 'x')",
            user_id,
            f"informed-{user_id.hex[:8]}@demo",
            "Ivy Informed",
        )
        await seeded.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id) "
            "VALUES ($1, 'informed', 'team', $2)",
            user_id,
            stable_id("team", team_slug),
        )
        await seeded.execute(
            "INSERT INTO personal_tokens (id, user_id, token_hash, name) VALUES ($1, $2, $3, $4)",
            uuid.uuid4(),
            user_id,
            token_hash,
            name,
        )
        return SimpleNamespace(token=token, user_id=user_id, name=name)

    yield factory
    await seeded.execute("DELETE FROM users WHERE id = ANY($1)", created)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def connect(server_url):
    """`async with connect(token) as client:` -> connected `mcp.Client`."""

    @asynccontextmanager
    async def _connect(token: str) -> AsyncIterator[Client]:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=60.0)) as http:
            transport = streamable_http_client(f"{server_url}/mcp", http_client=http)
            async with Client(transport) as mcp_client:
                yield mcp_client

    return _connect


@pytest.fixture(autouse=True)
def quiet_approval_notifications(monkeypatch):
    """Approver SMS/email go through channel adapters (covered in tests/runbooks); not needed here."""

    async def send(conn, row, alert, person):
        return None

    monkeypatch.setattr("app.runbooks.approvals._send", send)


@pytest_asyncio.fixture(loop_scope="session")
async def start_worker(seeded, redis, rabbit):
    """`await start_worker()` -> a real worker (real rule index and templates) on `events.in`."""
    started: list[tuple[Rabbit, Worker]] = []

    async def start() -> Worker:
        await purge_events_queue(rabbit)
        index = RuleIndex()
        await index.load_all(seeded)
        consumer = Rabbit()
        await consumer.connect()
        worker = Worker(seeded, consumer, redis, index=index, render=render)
        await worker.start()
        started.append((consumer, worker))
        return worker

    yield start
    for consumer, worker in started:
        await consumer.close()
        await worker.close()
    await purge_events_queue(rabbit)


@pytest.fixture(autouse=True)
def llm():
    """The stub LLM for every MCP test (product code uses the real client; tests never reach a provider)."""
    fake = FakeLLM()
    set_llm(fake)
    yield fake
    set_llm(None)


@pytest.fixture(autouse=True)
def no_text_runbook_ai(monkeypatch):
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: False)


def text_of(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)
