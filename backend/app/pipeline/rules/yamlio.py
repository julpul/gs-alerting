"""YAML <-> rule JSON (round-trip). `safe_load` / `safe_dump` only; YAML aliases are refused."""

from __future__ import annotations

from typing import Any

import yaml

from app.pipeline.rules.model import RuleValidationError

MAX_YAML_BYTES = 100_000


class _NoAliasLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise RuleValidationError("", "YAML anchors/aliases are not allowed")
        return super().compose_node(parent, index)


def dump_rule_yaml(rule_json: dict[str, Any]) -> str:
    return yaml.safe_dump(rule_json, sort_keys=False, allow_unicode=True, default_flow_style=False)


def load_rule_yaml(text: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_YAML_BYTES:
        raise RuleValidationError("", f"YAML longer than {MAX_YAML_BYTES} bytes")
    try:
        data = yaml.load(text, Loader=_NoAliasLoader)
    except yaml.YAMLError as exc:
        raise RuleValidationError("", f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RuleValidationError("", "YAML document must be a mapping")
    return data
