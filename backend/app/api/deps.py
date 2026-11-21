"""Shared FastAPI dependencies for routers."""

from __future__ import annotations

from typing import Annotated

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, Request

from app.core.rabbit import Rabbit
from app.core.scope import CurrentScope, CurrentUser, Scope, current_scope, current_user
from app.schemas.internal import User


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_rabbit(request: Request) -> Rabbit:
    return request.app.state.rabbit


def get_redis_client(request: Request) -> aioredis.Redis:
    return request.app.state.redis


Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
RabbitDep = Annotated[Rabbit, Depends(get_rabbit)]
RedisDep = Annotated[aioredis.Redis, Depends(get_redis_client)]

__all__ = [
    "CurrentScope",
    "CurrentUser",
    "Pool",
    "RabbitDep",
    "RedisDep",
    "Scope",
    "User",
    "current_scope",
    "current_user",
    "get_pool",
    "get_rabbit",
    "get_redis_client",
]
