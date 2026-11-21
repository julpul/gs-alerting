"""`{{path}}` substitution (spec 7.4). Nothing is ever evaluated: a placeholder is a lookup in the event.

Event content is untrusted, so there is no Jinja, no attribute access, no filters. A path is looked up with
`EventView.lookup` (raw JSON or `_meta`); dunder-style paths are plain dict keys and simply resolve to
nothing. Missing -> empty string; a wildcard path (`legs.*.pair`) renders its values joined with ", ".
`html=True` escapes substituted values (never the template itself).
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import quote

import orjson

from app.pipeline.fields import MISSING, AnyOf, EventView

ELLIPSIS = "…"
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]{1,200}?)\s*\}\}")


def stringify(value: Any) -> str:
    if value is MISSING or value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, AnyOf):
        return ", ".join(stringify(item) for item in value)
    if isinstance(value, dict | list):
        return orjson.dumps(value).decode()
    return str(value)


def render(template: str, ev: EventView, *, html: bool = False, url: bool = False, max_len: int = 500) -> str:
    """Render `template` against the event; the result is at most `max_len` characters.
    `url=True` percent-encodes substituted values (never the template itself)."""
    if max_len <= 0:
        return ""
    pieces: list[tuple[str, bool]] = []
    pos = 0
    for m in _PLACEHOLDER.finditer(template):
        if m.start() > pos:
            pieces.append((template[pos : m.start()], False))
        pieces.append((stringify(ev.lookup(m.group(1))), True))
        pos = m.end()
    if pos < len(template):
        pieces.append((template[pos:], False))

    total = sum(len(text) for text, _ in pieces)
    budget = max_len if total <= max_len else max(max_len - 1, 0)
    out: list[str] = []
    for text, is_value in pieces:
        if budget <= 0:
            break
        text = text[:budget]
        budget -= len(text)
        if is_value and url:
            text = quote(text, safe="")
        out.append(html_lib.escape(text, quote=True) if html and is_value else text)
    if total > max_len:
        out.append(ELLIPSIS)
    return "".join(out)
