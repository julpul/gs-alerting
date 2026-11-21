"""Tool registry: one implementation per tool, exposed over MCP and to the in-app copilot.

A tool is `async def name(tc: ToolContext, **arguments) -> dict` raising `AppError` for expected failures.
`mcp_function` turns it into the callable the MCP SDK introspects (arguments become the input schema, `tc` is
built from the authenticated request), `json_schema` gives the same schema to the copilot prompt.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import orjson
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool

from app.core.errors import AppError
from app.mcp.context import ToolContext

ToolFn = Callable[..., Awaitable[dict[str, Any]]]
ContextFactory = Callable[[Context], ToolContext]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: ToolFn
    description: str
    writes: bool
    chat: bool


TOOLS: dict[str, ToolSpec] = {}


def tool(*, writes: bool = False, chat: bool = True) -> Callable[[ToolFn], ToolFn]:
    """Register a tool. `writes`: changes state (informational); `chat`: also offered to the copilot."""

    def register(fn: ToolFn) -> ToolFn:
        TOOLS[fn.__name__] = ToolSpec(fn.__name__, fn, inspect.cleandoc(fn.__doc__ or ""), writes, chat)
        return fn

    return register


def jsonable(value: Any) -> Any:
    """Plain JSON types (datetimes, UUIDs, enums) for MCP structured content and LLM prompts."""
    return orjson.loads(orjson.dumps(value, default=str, option=orjson.OPT_UTC_Z | orjson.OPT_NON_STR_KEYS))


def tool_error(exc: AppError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.message}")


def mcp_function(spec: ToolSpec, make_context: ContextFactory) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The function registered on the MCP server: spec signature minus `tc`, plus the injected `ctx`."""
    signature = inspect.signature(spec.fn)
    params = [p for name, p in signature.parameters.items() if name != "tc"]
    params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))

    async def call(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs.pop("ctx")
        try:
            return jsonable(await spec.fn(make_context(ctx), **kwargs))
        except AppError as exc:
            raise tool_error(exc) from exc

    call.__name__ = spec.name
    call.__doc__ = spec.description
    call.__signature__ = signature.replace(parameters=params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    call.__annotations__ = {
        **{p.name: p.annotation for p in params},
        "return": dict[str, Any],
    }
    return call


def json_schema(spec: ToolSpec) -> dict[str, Any]:
    """JSON schema of the tool arguments (the same one MCP clients see)."""

    def unused(_: Context) -> ToolContext:
        raise RuntimeError("schema only")  # pragma: no cover

    return Tool.from_function(mcp_function(spec, unused), name=spec.name).parameters
