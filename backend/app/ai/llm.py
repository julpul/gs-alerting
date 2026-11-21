"""One LLM client over the LiteLLM Router (spec 13.6).

* Two model slots (`LLM_MODEL_FAST`, `LLM_MODEL_SMART`) plus the `LLM_FALLBACKS` chain; providers without an
  API key are skipped. Per call: 20 s timeout, 1 retry, then the next model in the chain.
* Structured output: a JSON schema derived from a pydantic model, the answer is validated and gets one
  repair round before the call degrades.
* `AIUnavailable` is raised when nothing is configured or every provider failed; callers degrade.
* The client is injected (`set_llm`) so tests use `tests/fakes/llm.py`. Product code never imports the fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.core.security import decrypt_secret

log = logging.getLogger(__name__)

Tier = Literal["fast", "smart"]

CALL_TIMEOUT_S = 20.0
RETRIES = 1
MAX_OUTPUT_TOKENS = 2000
OVERALL_TIMEOUT_S = 75.0

PROVIDER_KEYS = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "openrouter": "openrouter_api_key",
}
DEFAULT_MODELS: dict[str, dict[Tier, str]] = {
    "anthropic": {"fast": "anthropic/claude-haiku-5-5", "smart": "anthropic/claude-sonnet-5-5"},
    "openai": {"fast": "openai/gpt-5-mini", "smart": "openai/gpt-5"},
    "openrouter": {
        "fast": "openrouter/anthropic/claude-haiku-5-5",
        "smart": "openrouter/anthropic/claude-sonnet-5-5",
    },
}


class AIUnavailable(Exception):
    """No provider configured, or every provider failed / answered with invalid output."""


class LLMResult(NamedTuple):
    parsed: Any
    model: str
    latency_ms: int


@dataclass(frozen=True)
class LLMOverride:
    """Per-domain provider configuration (`integrations` kind `llm`)."""

    mode: str
    model: str
    api_key: str | None = None
    base_url: str | None = None

    def litellm_model(self) -> str:
        if self.mode == "own_endpoint" and "/" not in self.model:
            return f"openai/{self.model}"
        return self.model


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        tier: Tier = "fast",
        override: LLMOverride | None = None,
    ) -> LLMResult: ...


def _provider(model: str) -> str:
    return model.split("/", 1)[0]


def _has_key(settings: Settings, model: str) -> bool:
    attr = PROVIDER_KEYS.get(_provider(model))
    return True if attr is None else bool(getattr(settings, attr))


def _default_model(settings: Settings, tier: Tier) -> str | None:
    for provider, models in DEFAULT_MODELS.items():
        if getattr(settings, PROVIDER_KEYS[provider]):
            return models[tier]
    return None


def model_chains(settings: Settings) -> dict[Tier, list[str]]:
    """Ordered model strings per tier (primary first, then fallbacks), providers without a key skipped."""
    configured = {"fast": settings.llm_model_fast.strip(), "smart": settings.llm_model_smart.strip()}
    fallbacks = [m.strip() for m in settings.llm_fallbacks.split(",") if m.strip()]
    chains: dict[Tier, list[str]] = {"fast": [], "smart": []}
    for tier in chains:
        primary = configured[tier] or _default_model(settings, tier)
        for model in [primary, *fallbacks]:
            if model and model not in chains[tier] and _has_key(settings, model):
                chains[tier].append(model)
    return chains


def router_config(settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, list[str]]]]:
    """LiteLLM Router `model_list` and `fallbacks`: deployment `<tier>-<n>`, fallbacks keep chain order."""
    model_list: list[dict[str, Any]] = []
    fallbacks: list[dict[str, list[str]]] = []
    for tier, chain in model_chains(settings).items():
        for index, model in enumerate(chain):
            attr = PROVIDER_KEYS.get(_provider(model))
            params: dict[str, Any] = {"model": model, "timeout": CALL_TIMEOUT_S}
            if attr:
                params["api_key"] = getattr(settings, attr)
            name = tier if index == 0 else f"{tier}-fb{index}"
            model_list.append(
                {"model_name": name, "litellm_params": params, "model_info": {"id": f"{tier}:{model}"}}
            )
        if len(chain) > 1:
            fallbacks.append({tier: [f"{tier}-fb{i}" for i in range(1, len(chain))]})
    return model_list, fallbacks


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse a model answer: plain JSON, fenced JSON, or JSON embedded in prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def response_format(schema: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False},
    }


class LiteLLMClient:
    """Real client. `router` may be injected (tests) instead of the lazily built LiteLLM Router."""

    def __init__(self, settings: Settings | None = None, *, router: Any | None = None) -> None:
        self._settings = settings
        self._router = router

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def configured(self) -> bool:
        return any(model_chains(self.settings).values())

    def _get_router(self) -> Any:
        if self._router is None:
            import litellm
            from litellm import Router

            litellm.drop_params = True
            litellm.suppress_debug_info = True
            model_list, fallbacks = router_config(self.settings)
            if not model_list:
                raise AIUnavailable("no LLM provider configured (set a provider API key)")
            self._router = Router(
                model_list=model_list, fallbacks=fallbacks, num_retries=RETRIES, timeout=CALL_TIMEOUT_S
            )
        return self._router

    async def _call(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        tier: Tier,
        override: LLMOverride | None,
    ) -> tuple[str, str]:
        fmt = response_format(schema)
        try:
            if override is not None:
                import litellm

                litellm.drop_params = True
                resp = await asyncio.wait_for(
                    litellm.acompletion(
                        model=override.litellm_model(),
                        messages=messages,
                        response_format=fmt,
                        api_key=override.api_key,
                        api_base=override.base_url,
                        timeout=CALL_TIMEOUT_S,
                        num_retries=RETRIES,
                        max_tokens=MAX_OUTPUT_TOKENS,
                    ),
                    OVERALL_TIMEOUT_S,
                )
                used = override.litellm_model()
            else:
                resp = await asyncio.wait_for(
                    self._get_router().acompletion(
                        model=tier, messages=messages, response_format=fmt, max_tokens=MAX_OUTPUT_TOKENS
                    ),
                    OVERALL_TIMEOUT_S,
                )
                used = _answering_model(resp)
        except AIUnavailable:
            raise
        except Exception as exc:
            raise AIUnavailable(f"LLM call failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
        content = resp.choices[0].message.content
        if not content:
            raise AIUnavailable("LLM returned an empty answer")
        return content, used

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        tier: Tier = "fast",
        override: LLMOverride | None = None,
    ) -> LLMResult:
        started = time.perf_counter()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in (1, 2):
            content, used = await self._call(messages, schema, tier, override)
            try:
                parsed = schema.model_validate(extract_json(content))
            except (ValueError, ValidationError) as exc:
                if attempt == 2:
                    raise AIUnavailable("LLM answer failed schema validation after a repair retry") from exc
                log.info("LLM answer invalid (%s); asking for a repair", type(exc).__name__)
                messages = [
                    *messages,
                    {"role": "assistant", "content": content[:4000]},
                    {
                        "role": "user",
                        "content": "Your answer was not valid JSON for the required schema "
                        f"({str(exc)[:300]}). Reply again with only the corrected JSON object.",
                    },
                ]
                continue
            return LLMResult(parsed, used, int((time.perf_counter() - started) * 1000))
        raise AIUnavailable("unreachable")  # pragma: no cover


def _answering_model(resp: Any) -> str:
    """The deployment that answered (`tier:model` id set in `router_config`), else the response model."""
    hidden = getattr(resp, "_hidden_params", None) or {}
    deployment = hidden.get("model_id") if isinstance(hidden, dict) else None
    if isinstance(deployment, str) and ":" in deployment:
        return deployment.split(":", 1)[1]
    return str(getattr(resp, "model", None) or "unknown")


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LiteLLMClient()
    return _client


def set_llm(client: LLMClient | None) -> None:
    """Inject a client (tests) or reset to the real one (`None`)."""
    global _client
    _client = client


def llm_configured() -> bool:
    """True when the platform default has at least one usable provider."""
    return any(model_chains(get_settings()).values())


async def domain_override(conn: asyncpg.Connection, domain_id: UUID | None) -> LLMOverride | None:
    """The domain's own LLM configuration (decrypted), or None for the platform default."""
    if domain_id is None:
        return None
    row = await conn.fetchrow(
        "SELECT mode, config_encrypted FROM integrations WHERE domain_id = $1 AND kind = 'llm'", domain_id
    )
    if row is None or row["mode"] not in ("own_key", "own_endpoint") or not row["config_encrypted"]:
        return None
    try:
        cfg = json.loads(decrypt_secret(row["config_encrypted"]))
    except Exception:
        log.warning("cannot decrypt llm integration of domain %s", domain_id)
        return None
    if not cfg.get("model"):
        return None
    return LLMOverride(row["mode"], cfg["model"], cfg.get("apiKey"), cfg.get("baseUrl"))


async def complete_for_domain(
    conn: asyncpg.Connection,
    domain_id: UUID | None,
    *,
    system: str,
    user: str,
    schema: type[BaseModel],
    tier: Tier = "fast",
) -> LLMResult:
    """`get_llm().complete` honouring the domain's own provider configuration."""
    override = await domain_override(conn, domain_id)
    return await get_llm().complete(system=system, user=user, schema=schema, tier=tier, override=override)
