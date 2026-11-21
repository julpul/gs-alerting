"""Process roles. One image, many roles (`python -m app.main --role worker,delivery`)."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable

API_ROLE = "api"

# role -> "module:function" of an `async def run(stop: asyncio.Event)`; loaded lazily.
ROLE_TARGETS: dict[str, str] = {
    "worker": "app.pipeline.worker:run",
    "delivery": "app.delivery.worker:run",
    "scheduler": "app.escalation.scheduler:run",
    "ai": "app.ai.worker:run",
    "connector": "app.connectors.runner:run",
}

ALL_ROLES: tuple[str, ...] = (API_ROLE, *ROLE_TARGETS)


def parse_roles(spec: str) -> list[str]:
    """Expand `api`, `worker,delivery` or `all` into an ordered, de-duplicated role list."""
    roles: list[str] = []
    for part in (p.strip().lower() for p in spec.split(",")):
        if not part:
            continue
        expanded = list(ALL_ROLES) if part == "all" else [part]
        for role in expanded:
            if role not in ALL_ROLES:
                raise ValueError(f"unknown role {role!r}; valid: {', '.join(ALL_ROLES)}, all")
            if role not in roles:
                roles.append(role)
    if not roles:
        raise ValueError("no role given")
    return roles


def load_runner(role: str) -> Callable[[asyncio.Event], Awaitable[None]]:
    module_name, _, func_name = ROLE_TARGETS[role].partition(":")
    return getattr(importlib.import_module(module_name), func_name)
