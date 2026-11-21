"""Markdown pages and plain checklists (`- [ ] item`): numbered or bulleted items become steps."""

from __future__ import annotations

import re

from app.runbooks.model import ParsedRunbook, finalize
from app.runbooks.parsers.heuristics import step_from_sentence

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_ITEM = re.compile(r"^(\s*)(?:\d+[.)]|[-*+])\s+(?:\[[ xX]?\]\s+)?(.*\S)\s*$")


def has_items(content: str) -> bool:
    return any(_ITEM.match(line) for line in content.splitlines())


def _items(content: str) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        m = _ITEM.match(line)
        if m:
            items.append(m.group(2))
        elif line.strip() and items and line[:1] in " \t" and not _HEADING.match(line):
            items[-1] += " " + line.strip()
    return items


def title_of(content: str) -> str | None:
    for line in content.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(1).strip()
    return None


def parse(content: str) -> ParsedRunbook | None:
    """None when the page has no list items (the caller falls back to the free-text parser)."""
    items = _items(content)
    if not items:
        return None
    steps = finalize([step_from_sentence(item) for item in items])
    suggested = any(s.suggested for s in steps)
    return ParsedRunbook(
        name=title_of(content) or "Runbook",
        format="markdown",
        steps=steps,
        parsed_by="heuristic" if suggested else "deterministic",
    )
