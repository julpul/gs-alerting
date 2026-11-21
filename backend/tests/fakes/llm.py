"""Stub LLM client for tests (product code always uses the real LLM; it never imports this module)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from app.ai.llm import AIUnavailable, LLMResult

Responses = list[Any] | dict[str, Any] | Callable[[str, str], Any] | None


class FakeLLM:
    """Returns canned JSON; records every call.

    `responses` may be a list (consumed in order; an `Exception` item is raised), a dict keyed by model
    tier, or a callable `(prompt, tier) -> dict`. `fail=True` makes every call raise `AIUnavailable`.
    Implements both the product interface (`complete`) and the older `complete_json`.
    """

    def __init__(self, responses: Responses = None, *, model: str = "fake-model", fail: bool = False):
        self.responses = responses if responses is not None else {}
        self.model = model
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def _next(self, prompt: str, tier: str) -> Any:
        if callable(self.responses):
            return self.responses(prompt, tier)
        if isinstance(self.responses, list):
            return self.responses.pop(0)
        return self.responses.get(tier, {})

    async def complete_json(self, prompt: str, *, tier: str = "fast", schema: dict | None = None) -> dict:
        self.calls.append({"prompt": prompt, "tier": tier, "schema": schema})
        return self._next(prompt, tier)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        tier: str = "fast",
        override: Any = None,
    ) -> LLMResult:
        prompt = f"{system}\n\n{user}"
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "user": user,
                "tier": tier,
                "schema": schema,
                "override": override,
            }
        )
        if self.fail:
            raise AIUnavailable("fake LLM is down")
        data = self._next(prompt, tier)
        if isinstance(data, Exception):
            raise data
        try:
            parsed = schema.model_validate(data)
        except ValidationError as exc:
            raise AIUnavailable("fake answer failed validation") from exc
        return LLMResult(parsed, self.model, 5)

    @property
    def prompts(self) -> list[str]:
        return [c["prompt"] for c in self.calls]
