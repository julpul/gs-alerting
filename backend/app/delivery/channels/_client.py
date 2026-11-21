"""Shared httpx client for provider calls (one per event loop; 3 s total timeout)."""

from __future__ import annotations

import asyncio

import httpx

SEND_TIMEOUT_S = 3.0

_clients: dict[int, httpx.AsyncClient] = {}


def http_client() -> httpx.AsyncClient:
    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=SEND_TIMEOUT_S)
        _clients[loop_id] = client
    return client


async def close_clients() -> None:
    for client in list(_clients.values()):
        if not client.is_closed:
            await client.aclose()
    _clients.clear()
