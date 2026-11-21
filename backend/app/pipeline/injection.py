"""Poisoned-telemetry detector (spec 13.4): deterministic regex scoring over every string field."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FLAG = "suspicious_content"
THRESHOLD = 0.7
MAX_STRINGS = 300
MAX_CHARS = 4096
MAX_DEPTH = 8

_INSTRUCTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)"
    r"|disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above|instructions)"
    r"|you\s+are\s+now\b|new\s+instructions?\b|system\s+prompt"
    r"|(?:^|\W)(?:assistant|system)\s*:|<\|im_(?:start|end)\|>|forget\s+(?:everything|all)\b"
    r"|act\s+as\s+(?:an?\s+)?(?:admin|root|system)",
    re.IGNORECASE,
)
_COMMAND = re.compile(
    r"rm\s+-[a-z]*r[a-z]*f|(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b|kubectl\s+delete"
    r"|drop\s+(?:table|database)\b|chmod\s+(?:-r\s+)?777|\bmkfs\b|\bdd\s+if=|:\(\)\s*\{|/etc/passwd"
    r"|\bsudo\s+\w+|shutdown\s+-[hr]|systemctl\s+(?:stop|disable)",
    re.IGNORECASE,
)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")

_SIGNALS = (("instruction", 0.7, _INSTRUCTION), ("command", 0.7, _COMMAND), ("base64", 0.4, _BASE64_BLOB))


@dataclass
class InjectionResult:
    score: float = 0.0
    hits: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return self.score >= THRESHOLD


def _strings(value: Any) -> list[str]:
    out: list[str] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack and len(out) < MAX_STRINGS:
        current, level = stack.pop()
        if isinstance(current, str):
            out.append(current[:MAX_CHARS])
        elif level < MAX_DEPTH and isinstance(current, dict):
            stack.extend((v, level + 1) for v in current.values())
        elif level < MAX_DEPTH and isinstance(current, list):
            stack.extend((v, level + 1) for v in current)
    return out


def scan(raw: Any) -> InjectionResult:
    """Score = sum of the weights of the distinct signal kinds found anywhere in the event."""
    found: dict[str, float] = {}
    for text in _strings(raw):
        for name, weight, pattern in _SIGNALS:
            if name not in found and pattern.search(text):
                found[name] = weight
        if len(found) == len(_SIGNALS):
            break
    return InjectionResult(score=round(sum(found.values()), 2), hits=sorted(found))
