"""Fixtures for the delivery tests: seeded database, in-process mockgw on a free port, delivery worker."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio
import uvicorn

MOCKGW_DIR = Path(__file__).resolve().parents[3] / "mockgw"


class _Server(uvicorn.Server):
    def capture_signals(self):  # the test runner owns signal handling
        import contextlib

        return contextlib.nullcontext()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def mockgw_server(session_env) -> AsyncIterator[str]:
    """Run the real mockgw app with uvicorn on a free port and point MOCKGW_URL at it."""
    from app.config import get_settings

    sys.path.insert(0, str(MOCKGW_DIR))
    spec = importlib.util.spec_from_file_location("mockgw_app", MOCKGW_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    port = _free_port()
    server = _Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    previous = os.environ.get("MOCKGW_URL")
    os.environ["MOCKGW_URL"] = f"http://127.0.0.1:{port}"
    get_settings.cache_clear()
    try:
        yield os.environ["MOCKGW_URL"]
    finally:
        server.should_exit = True
        await task
        if previous is None:
            os.environ.pop("MOCKGW_URL", None)
        else:
            os.environ["MOCKGW_URL"] = previous
        get_settings.cache_clear()
        sys.path.remove(str(MOCKGW_DIR))


@pytest_asyncio.fixture(loop_scope="session")
async def gw(mockgw_server) -> AsyncIterator[httpx.AsyncClient]:
    """Client for the mockgw admin API; state is reset before and after each test."""
    async with httpx.AsyncClient(base_url=mockgw_server, timeout=5) as client:
        await client.post("/admin/reset")
        yield client
        await client.post("/admin/reset")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def worker(seeded, rabbit, mockgw_server):
    """A delivery worker consuming the (prefixed) deliveries queues of this test session."""
    from app.delivery.worker import DeliveryWorker

    w = DeliveryWorker(seeded, rabbit)
    await w.start()
    yield w
    from app.delivery.channels._client import close_clients

    await close_clients()
