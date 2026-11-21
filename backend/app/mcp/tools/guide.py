"""`get_integration_guide`: how to talk to this service (the full contract is the resource `docs://api`)."""

from typing import Any

from app.mcp.context import ToolContext
from app.mcp.guide import RESOURCE_URI, endpoint_index, guide_markdown
from app.mcp.registry import tool


@tool(chat=False)
async def get_integration_guide(tc: ToolContext) -> dict[str, Any]:
    """Markdown guide with curl/python examples and an index of the REST endpoints. Read the MCP resource
    `docs://api` for the complete OpenAPI contract."""
    return {"guide": guide_markdown(), "endpoints": endpoint_index(), "openapiResource": RESOURCE_URI}
