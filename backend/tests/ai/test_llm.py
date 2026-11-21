"""LLM client: model slots, provider fallback order, structured output + repair, AIUnavailable, overrides."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest
from litellm import Router
from pydantic import BaseModel

from app.ai import llm as llm_mod
from app.ai.llm import (
    AIUnavailable,
    LiteLLMClient,
    LLMOverride,
    complete_for_domain,
    domain_override,
    extract_json,
    model_chains,
    router_config,
    set_llm,
)
from app.config import Settings
from app.core.security import encrypt_secret
from tests.api.alerts_helpers import domain
from tests.fakes.llm import FakeLLM


class Answer(BaseModel):
    value: int
    label: str = ""


def settings(**kw: Any) -> Settings:
    base = {"anthropic_api_key": "", "openai_api_key": "", "openrouter_api_key": "", "llm_fallbacks": ""}
    return Settings(**{**base, **kw}, _env_file=None)


class ScriptedRouter:
    """Stands in for the LiteLLM Router: answers are consumed in order (exceptions are raised)."""

    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def acompletion(self, **kwargs: Any):
        self.calls.append(kwargs)
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = SimpleNamespace(content=item)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            model="provider/model",
            _hidden_params={"model_id": "fast:x/y"},
        )


class ForcedFallbackRouter:
    """A real LiteLLM Router whose primary deployment fails (LiteLLM's mock fallback switch)."""

    def __init__(self, cfg: Settings):
        model_list, fallbacks = router_config(cfg)
        for entry in model_list:
            entry["litellm_params"]["mock_response"] = '{"value": 7}'
        self.router = Router(model_list=model_list, fallbacks=fallbacks, num_retries=0, timeout=5)

    async def acompletion(self, **kwargs: Any):
        return await self.router.acompletion(**kwargs, mock_testing_fallbacks=True)


def test_slots_defaults_and_providers_without_key_are_skipped():
    assert model_chains(settings()) == {"fast": [], "smart": []}
    only_anthropic = model_chains(settings(anthropic_api_key="k"))
    assert only_anthropic["fast"] == ["anthropic/claude-haiku-5-5"]
    assert only_anthropic["smart"] == ["anthropic/claude-sonnet-5-5"]
    explicit = settings(
        openai_api_key="k",
        llm_model_fast="anthropic/claude-haiku-5-5",
        llm_model_smart="openai/big",
        llm_fallbacks="anthropic/x, openrouter/v/m ,openai/small",
    )
    chains = model_chains(explicit)
    assert chains["fast"] == ["openai/small"]  # anthropic + openrouter have no key
    assert chains["smart"] == ["openai/big", "openai/small"]


def test_fallback_order_follows_the_configuration():
    cfg = settings(
        anthropic_api_key="a",
        openai_api_key="o",
        openrouter_api_key="r",
        llm_model_fast="anthropic/claude-haiku-5-5",
        llm_model_smart="anthropic/claude-sonnet-5-5",
        llm_fallbacks="openai/gpt-5-mini,openrouter/anthropic/claude-haiku-5-5",
    )
    assert model_chains(cfg)["fast"] == [
        "anthropic/claude-haiku-5-5",
        "openai/gpt-5-mini",
        "openrouter/anthropic/claude-haiku-5-5",
    ]
    model_list, fallbacks = router_config(cfg)
    fast = [e for e in model_list if e["model_name"].startswith("fast")]
    assert [e["litellm_params"]["model"] for e in fast] == model_chains(cfg)["fast"]
    assert {"fast": ["fast-fb1", "fast-fb2"]} in fallbacks
    assert fast[1]["litellm_params"]["api_key"] == "o" and fast[0]["litellm_params"]["timeout"] == 20.0


async def test_real_router_falls_back_in_configured_order_and_reports_the_model():
    cfg = settings(anthropic_api_key="a", openai_api_key="o", llm_fallbacks="openai/gpt-5-mini")
    client = LiteLLMClient(cfg, router=ForcedFallbackRouter(cfg))
    result = await client.complete(system="s", user="u", schema=Answer, tier="fast")
    assert result.parsed.value == 7
    assert result.model == "openai/gpt-5-mini"  # the primary (anthropic) was skipped, the fallback answered
    assert result.latency_ms >= 0


async def test_structured_output_sends_the_json_schema_and_validates():
    router = ScriptedRouter('```json\n{"value": 3, "label": "x"}\n```')
    client = LiteLLMClient(settings(anthropic_api_key="k"), router=router)
    result = await client.complete(system="sys", user="usr", schema=Answer, tier="smart")
    assert result.parsed == Answer(value=3, label="x") and result.model == "x/y"
    call = router.calls[0]
    assert call["model"] == "smart" and call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["schema"]["properties"]["value"]
    assert [m["role"] for m in call["messages"]] == ["system", "user"]


async def test_one_repair_retry_then_success_and_failure():
    router = ScriptedRouter("not json at all", '{"value": 1}')
    client = LiteLLMClient(settings(anthropic_api_key="k"), router=router)
    result = await client.complete(system="s", user="u", schema=Answer)
    assert result.parsed.value == 1 and len(router.calls) == 2
    repair = router.calls[1]["messages"]
    assert repair[-2]["role"] == "assistant" and "corrected JSON" in repair[-1]["content"]

    bad = ScriptedRouter("nope", '{"value": "NaN"}')
    with pytest.raises(AIUnavailable, match="schema validation"):
        await LiteLLMClient(settings(anthropic_api_key="k"), router=bad).complete(
            system="s", user="u", schema=Answer
        )
    assert len(bad.calls) == 2


async def test_provider_failure_and_missing_configuration_raise_ai_unavailable():
    failing = ScriptedRouter(RuntimeError("503 from provider"))
    with pytest.raises(AIUnavailable, match="503 from provider"):
        await LiteLLMClient(settings(anthropic_api_key="k"), router=failing).complete(
            system="s", user="u", schema=Answer
        )
    empty = ScriptedRouter("")
    with pytest.raises(AIUnavailable, match="empty"):
        await LiteLLMClient(settings(anthropic_api_key="k"), router=empty).complete(
            system="s", user="u", schema=Answer
        )
    with pytest.raises(AIUnavailable, match="no LLM provider configured"):
        await LiteLLMClient(settings()).complete(system="s", user="u", schema=Answer)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no braces")


async def test_domain_override_is_decrypted_and_forwarded(pool):
    domain_id = domain("settlements")
    cfg = {
        "mode": "own_endpoint",
        "model": "llama3",
        "baseUrl": "http://llm.internal:8000/v1",
        "apiKey": "sek",
    }
    await pool.execute(
        "INSERT INTO integrations (domain_id, kind, config_encrypted, mode) "
        "VALUES ($1, 'llm', $2, 'own_endpoint') "
        "ON CONFLICT (domain_id, kind) DO UPDATE SET config_encrypted = $2, mode = 'own_endpoint'",
        domain_id,
        encrypt_secret(json.dumps(cfg)),
    )
    try:
        async with pool.acquire() as conn:
            override = await domain_override(conn, domain_id)
            assert override == LLMOverride("own_endpoint", "llama3", "sek", "http://llm.internal:8000/v1")
            assert override.litellm_model() == "openai/llama3"
            assert await domain_override(conn, domain("payments")) is None
            assert await domain_override(conn, None) is None
            fake = FakeLLM({"fast": {"value": 1}})
            set_llm(fake)
            await complete_for_domain(conn, domain_id, system="s", user="u", schema=Answer)
            assert fake.calls[0]["override"] == override
    finally:
        await pool.execute("DELETE FROM integrations WHERE domain_id = $1 AND kind = 'llm'", domain_id)


async def test_override_call_uses_litellm_directly(monkeypatch):
    seen: dict[str, Any] = {}

    async def acompletion(**kwargs: Any):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"value": 2}'))])

    monkeypatch.setattr("litellm.acompletion", acompletion)
    override = LLMOverride("own_key", "anthropic/claude-haiku-5-5", "sk-own", None)
    result = await LiteLLMClient(settings()).complete(system="s", user="u", schema=Answer, override=override)
    assert result.parsed.value == 2 and result.model == "anthropic/claude-haiku-5-5"
    assert seen["api_key"] == "sk-own" and seen["timeout"] == 20.0 and seen["num_retries"] == 1


def test_get_llm_returns_the_real_client_unless_injected():
    set_llm(None)
    assert isinstance(llm_mod.get_llm(), LiteLLMClient)
    fake = FakeLLM()
    set_llm(fake)
    assert llm_mod.get_llm() is fake


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    reason="needs ANTHROPIC_API_KEY or OPENAI_API_KEY",
)
async def test_live_provider_returns_structured_output():
    set_llm(None)
    client = LiteLLMClient(Settings(_env_file=None))
    result = await client.complete(
        system="Answer with JSON only.", user='Return {"value": 42, "label": "ok"}.', schema=Answer
    )
    assert result.parsed.value == 42 and result.model


class OpenAnswer(BaseModel):
    rule: dict[str, Any]


async def test_open_dict_schemas_go_into_the_prompt_not_the_provider_schema():
    """Anthropic structured output turns `dict[str, Any]` into `{}`, so the model could not fill it."""
    router = ScriptedRouter('{"rule": {"name": "r", "match": {"field": "a"}}}')
    client = LiteLLMClient(settings(anthropic_api_key="k"), router=router)
    result = await client.complete(system="sys", user="usr", schema=OpenAnswer, tier="smart")
    assert result.parsed.rule == {"name": "r", "match": {"field": "a"}}
    call = router.calls[0]
    assert "response_format" not in call
    assert call["messages"][0]["content"].startswith("sys") and '"rule"' in call["messages"][0]["content"]
