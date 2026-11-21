"""Markdown pages and plain checklists (`- [ ] item`): numbered or bulleted items become steps.

A table becomes one step per data row (never the header or the `|---|` separator): the cells are joined
as `Header: value; Header: value`, and a column named like "Approval" / "Sign-off" with a value other
than none / no / - makes the step approval: required.
"""

from __future__ import annotations

import re

from app.runbooks.model import ParsedRunbook, finalize
from app.runbooks.parsers.heuristics import step_from_sentence
from app.schemas import api as wire

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_ITEM = re.compile(r"^(\s*)(?:\d+[.)]|[-*+])\s+(?:\[[ xX]?\]\s+)?(.*\S)\s*$")
_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*(?::?-{2,}:?\s*)?\|?\s*$")
_APPROVAL_COLUMN = re.compile(r"approv|sign.?off|ok\b", re.IGNORECASE)
_NO_APPROVAL = {"", "none", "no", "-", "n/a", "na", "not required", "false"}


def has_items(content: str) -> bool:
    return any(_ITEM.match(line) or _ROW.match(line) for line in content.splitlines())


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


class _Table:
    def __init__(self) -> None:
        self.header: list[str] | None = None
        self.rows: list[list[str]] = []

    def add(self, line: str) -> None:
        if _SEPARATOR.match(line):
            return
        if self.header is None:
            self.header = _cells(line)
        else:
            self.rows.append(_cells(line))

    def steps(self) -> list[wire.RunbookStep]:
        header = self.header or []
        if not self.rows:  # a lone row is data, not a header
            self.rows, header = ([header] if header else []), []
        out = []
        for row in self.rows:
            pairs = [(header[i] if i < len(header) else "", cell) for i, cell in enumerate(row) if cell]
            if not pairs:
                continue
            text = "; ".join(f"{name}: {cell}" if name else cell for name, cell in pairs)
            step = step_from_sentence(text)
            if any(_APPROVAL_COLUMN.search(n) and c.lower() not in _NO_APPROVAL for n, c in pairs if n):
                step = step.model_copy(update={"approval": wire.Approval1.required})
            out.append(step)
        return out


def _steps(content: str) -> list[wire.RunbookStep]:
    steps: list[wire.RunbookStep] = []
    open_item = False
    table: _Table | None = None
    for line in content.splitlines():
        if _ROW.match(line):
            table = table or _Table()
            table.add(line)
            open_item = False
            continue
        if table is not None:
            steps += table.steps()
            table = None
        m = _ITEM.match(line)
        if m:
            steps.append(step_from_sentence(m.group(2)))
            open_item = True
        elif line.strip() and open_item and line[:1] in " \t" and not _HEADING.match(line):
            last = steps[-1]
            steps[-1] = step_from_sentence(f"{last.text} {line.strip()}")
        elif line.strip():
            open_item = False
    if table is not None:
        steps += table.steps()
    return steps


def title_of(content: str) -> str | None:
    for line in content.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(1).strip()
    return None


def parse(content: str) -> ParsedRunbook | None:
    """None when the page has no list items (the caller falls back to the free-text parser)."""
    found = _steps(content)
    if not found:
        return None
    steps = finalize(found)
    suggested = any(s.suggested for s in steps)
    return ParsedRunbook(
        name=title_of(content) or "Runbook",
        format="markdown",
        steps=steps,
        parsed_by="heuristic" if suggested else "deterministic",
    )
