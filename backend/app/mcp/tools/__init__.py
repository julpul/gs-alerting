"""Importing this package registers every tool in `app.mcp.registry.TOOLS`."""

from app.mcp.tools import alerts, approvals, drafts, guide

__all__ = ["alerts", "approvals", "drafts", "guide"]
