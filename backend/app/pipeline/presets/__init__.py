"""Source presets (spec 5.2): `name -> Preset(mapping, split, on_event)`.

* `mapping`   pre-filled `SchemaMapping` (`mappings.py`), applied by the worker like any accepted mapping.
* `split`     one webhook body -> the raw events to ingest (called by the ingest endpoints and connectors).
* `on_event`  inspects a raw event and may return an action; `RESOLVE` makes the worker resolve the open
              alert the event refers to instead of creating one (`autoresolve.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.pipeline.presets import alertmanager
from app.pipeline.presets.mappings import PRESET_MAPPINGS

RESOLVE = "resolve"

Split = Callable[[dict[str, Any]], list[dict[str, Any]]]
OnEvent = Callable[[dict[str, Any]], str | None]


def _passthrough(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [raw]


@dataclass(frozen=True)
class Preset:
    name: str
    mapping: dict[str, Any]
    split: Split = _passthrough
    on_event: OnEvent | None = None
    resolve_note: str | None = None


def _alertmanager_event(raw: dict[str, Any]) -> str | None:
    return RESOLVE if alertmanager.is_resolved(raw) else None


PRESETS: dict[str, Preset] = {
    name: Preset(name, mapping) for name, mapping in PRESET_MAPPINGS.items() if name != "alertmanager"
}
PRESETS["alertmanager"] = Preset(
    "alertmanager",
    PRESET_MAPPINGS["alertmanager"],
    split=alertmanager.split,
    on_event=_alertmanager_event,
    resolve_note="Resolved by source (Alertmanager status=resolved)",
)


def get_preset(name: str | None) -> Preset | None:
    return PRESETS.get(name or "")


def detect_preset(raw: dict[str, Any]) -> str | None:
    """Zero configuration: a source without a preset still recognises an Alertmanager webhook body."""
    return "alertmanager" if alertmanager.looks_like(raw) else None


def effective_preset(name: str | None, raw: dict[str, Any]) -> Preset | None:
    """The source's preset, else the one the body's shape gives away (`detect_preset`)."""
    return get_preset(name) or get_preset(detect_preset(raw))


def split_events(preset: str | None, raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The raw events a body of `preset` stands for; no preset and no recognised shape -> the body itself."""
    found = effective_preset(preset, raw)
    return found.split(raw) if found else [raw]
