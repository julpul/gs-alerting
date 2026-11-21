"""`RuleDef`: the contract `Rule` (docs/openapi.yaml) as a pydantic model.

The same JSON (camelCase) is the YAML form, the visual-builder payload, the AI copilot output and what is
stored in `rules.definition`. Python code uses snake_case attributes (`rule.presentation.title_template`).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, ValidationError, field_validator
from pydantic.alias_generators import to_camel

Operator = Literal[
    "eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "regex", "glob", "exists"
]
Severity = Literal["critical", "error", "warning", "info"]
Channel = Literal["ui", "email", "sms", "phone", "webhook"]
MAX_DEPTH = 6
MAX_CONDITIONS = 200

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class RuleValidationError(Exception):
    """A rule is invalid; `path` addresses the node, e.g. `match.conditions[1].operator`."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Condition(_Wire):
    field: Annotated[str, Field(min_length=1, max_length=200)]
    operator: Operator
    value: Any = None


def _node_kind(node: Any) -> str:
    if isinstance(node, dict):
        return "group" if "conditions" in node or "op" in node else "condition"
    return "group" if isinstance(node, ConditionGroup) else "condition"


class ConditionGroup(_Wire):
    op: Literal["and", "or"]
    conditions: list[ConditionNode]


ConditionNode = Annotated[
    Annotated[Condition, Tag("condition")] | Annotated[ConditionGroup, Tag("group")],
    Discriminator(_node_kind),
]
ConditionGroup.model_rebuild()


class Recipient(_Wire):
    kind: Literal["user", "team", "oncall", "role"]
    id: Annotated[str, Field(min_length=1, max_length=200)]


class TimeWindow(_Wire):
    from_: Annotated[str | None, Field(alias="from")] = None
    to: str | None = None
    timezone: str | None = None
    outside_channels: list[Channel] = Field(default_factory=list)

    @field_validator("from_", "to")
    @classmethod
    def _hhmm(cls, value: str | None) -> str | None:
        if value is not None and not _HHMM.match(value):
            raise ValueError("expected HH:MM (24 h)")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
                raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class Routing(_Wire):
    hard: list[Recipient] = Field(default_factory=list)
    soft: list[Recipient] = Field(default_factory=list)
    channels_by_severity: dict[Severity, list[Channel]] = Field(default_factory=dict)
    time_window: TimeWindow | None = None


class Presentation(_Wire):
    title_template: Annotated[str | None, Field(max_length=500)] = None
    body_template: Annotated[str | None, Field(max_length=5000)] = None
    fields: Annotated[list[str], Field(max_length=50)] = Field(default_factory=list)
    color: Annotated[str | None, Field(max_length=40)] = None


class Dedup(_Wire):
    fields: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    window_seconds: Annotated[int, Field(ge=1, le=86400)] = 300


class Grouping(_Wire):
    by_field: Annotated[str | None, Field(max_length=200)] = None
    window_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None


class Throttle(_Wire):
    max_per_recipient_per_minute: Annotated[int | None, Field(ge=1)] = None


class Escalation(_Wire):
    ack_timeout_seconds_by_severity: dict[Severity, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    path: list[Recipient] = Field(default_factory=list)


class RuleDef(_Wire):
    id: UUID | None = None
    version: int | None = None
    name: Annotated[str, Field(min_length=1, max_length=200)]
    team_id: UUID
    enabled: bool = True
    priority: Severity | None = None
    match: ConditionGroup
    routing: Routing = Field(default_factory=Routing)
    presentation: Presentation = Field(default_factory=Presentation)
    dedup: Dedup = Field(default_factory=Dedup)
    grouping: Grouping | None = None
    throttle: Throttle | None = None
    escalation: Escalation | None = None
    runbook_id: str | None = None

    def definition(self) -> dict[str, Any]:
        """The camelCase JSON stored in `rules.definition` (id/version live in columns)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"id", "version"})

    def to_wire(self) -> dict[str, Any]:
        out = self.definition()
        if self.id is not None:
            out["id"] = str(self.id)
        if self.version is not None:
            out["version"] = self.version
        return out


def format_path(loc: tuple[Any, ...]) -> str:
    """`('match','conditions',1,'operator')` -> `match.conditions[1].operator`."""
    out = ""
    for part in loc:
        if part in ("condition", "group"):
            continue
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def parse_rule(data: Any) -> RuleDef:
    """Validate a rule JSON; raises `RuleValidationError` pointing at the first invalid node."""
    if not isinstance(data, dict):
        raise RuleValidationError("", "rule must be an object")
    try:
        return RuleDef.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        message = err["msg"].removeprefix("Value error, ")
        raise RuleValidationError(format_path(err["loc"]), message) from exc
