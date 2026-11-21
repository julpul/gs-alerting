"""Who is calling a tool and with which infrastructure.

A `Principal` is a user's scope plus the actor written to audit/timeline rows: an agent acting on behalf of
that user (spec 13.1.3). Tool implementations never see HTTP; the MCP server and the in-app copilot both
build a `ToolContext` and call the same functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as aioredis

from app.core.rabbit import Rabbit
from app.core.scope import Scope
from app.schemas.internal import User

COPILOT_ID = "in-app-copilot"
COPILOT_NAME = "Copilot"


@dataclass(frozen=True)
class Principal:
    user: User
    scope: Scope
    actor: dict[str, Any]


def agent_actor(user: User, *, actor_id: str | UUID, name: str) -> dict[str, Any]:
    """Audited actor of an agent: `{type: agent, id, name, onBehalfOf, userId}`."""
    return {
        "type": "agent",
        "id": str(actor_id),
        "name": name,
        "onBehalfOf": user.display_name,
        "userId": str(user.id),
    }


def copilot_principal(user: User, scope: Scope) -> Principal:
    return Principal(user, scope, agent_actor(user, actor_id=COPILOT_ID, name=COPILOT_NAME))


@dataclass(frozen=True)
class ToolContext:
    pool: asyncpg.Pool
    rabbit: Rabbit
    redis: aioredis.Redis
    principal: Principal

    @property
    def user(self) -> User:
        return self.principal.user

    @property
    def scope(self) -> Scope:
        return self.principal.scope

    @property
    def actor(self) -> dict[str, Any]:
        return self.principal.actor
