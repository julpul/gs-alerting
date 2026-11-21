"""Stub LLM client for tests (product code always uses the real LLM; P3-AI extends this)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeLLM:
    """Returns canned JSON; records every call. `responses` may be a list (consumed in order),
    a dict keyed by model tier, or a callable `(prompt, tier) -> dict`."""

    def __init__(self, responses: list[dict] | dict[str, dict] | Callable[[str, str], dict] | None = None):
        self.responses = responses if responses is not None else {}
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, prompt: str, *, tier: str = "fast", schema: dict | None = None) -> dict:
        self.calls.append({"prompt": prompt, "tier": tier, "schema": schema})
        if callable(self.responses):
            return self.responses(prompt, tier)
        if isinstance(self.responses, list):
            return self.responses.pop(0)
        return self.responses.get(tier, {})
