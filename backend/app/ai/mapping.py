"""AI schema mapping (spec 6): the LLM proposes the field paths, every path must exist in the sample."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.ai import prompts
from app.ai.llm import AIUnavailable, get_llm
from app.pipeline.fields import MISSING, lookup
from app.pipeline.schema_detect import detect_schema

TARGETS = ("severity", "service", "environment", "timestamp", "title", "type", "error_code")
SEVERITIES = {"critical", "error", "warning", "info"}


class MappingAnswer(BaseModel):
    fields: dict[str, str | None] = Field(default_factory=dict)
    severityValueMap: dict[str, str] = Field(default_factory=dict)
    explanation: str = ""


def validate_mapping(answer: MappingAnswer, sample: dict[str, Any]) -> dict[str, Any]:
    """Keep only known targets whose path exists in the sample and severities from the allowed set."""
    fields: dict[str, str] = {}
    dropped: list[str] = []
    for target, path in answer.fields.items():
        if target not in TARGETS or not path:
            continue
        path = path.strip()
        if lookup(sample, path) is MISSING:
            dropped.append(f"{target} <- {path}")
            continue
        fields[target] = path
    severity_map = {
        str(k): v.lower() for k, v in answer.severityValueMap.items() if str(v).lower() in SEVERITIES
    }
    explanation = answer.explanation.strip()
    if dropped:
        explanation = f"{explanation} Dropped paths that are not in the sample: {', '.join(dropped)}.".strip()
    out: dict[str, Any] = {
        "detectedSchema": detect_schema(sample),
        "fields": fields,
        "explanation": explanation,
    }
    if severity_map:
        out["severityValueMap"] = severity_map
    return out


async def suggest_mapping(sample: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """A `SchemaMapping` draft (camelCase dict). Raises `AIUnavailable` when the model gave nothing usable."""
    system, user = prompts.mapping_prompt(sample, source)
    result = await get_llm().complete(system=system, user=user, schema=MappingAnswer, tier="fast")
    mapping = validate_mapping(result.parsed, sample)
    if not mapping["fields"]:
        raise AIUnavailable("the model proposed no usable field paths")
    return mapping
