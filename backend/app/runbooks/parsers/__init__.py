"""Runbook parsers: any supported format -> `ParsedRunbook` (steps in the contract `RunbookStep` shape).

Formats: `yaml` (also JSON `{steps: [...]}` / `{tasks: [...]}` ...), `markdown` (also plain `- [ ]`
checklists and tables), `text`, `link`. `detect_format` guesses; an explicitly requested format that does
not fit the content falls back to detection, so an unseen format still yields a draft instead of an error.
JSON in a shape we do not know becomes one step holding the formatted JSON (never split line by line).
"""

from __future__ import annotations

import re

from app.core.errors import AppError
from app.runbooks.model import MAX_CONTENT_BYTES, ParsedRunbook
from app.runbooks.parsers import link, markdown, text
from app.runbooks.parsers import yaml as structured

FORMATS = ("yaml", "markdown", "text", "link")
_STRUCTURED_KEY = re.compile(rf"^(?:runbook|{'|'.join(structured.STEP_LIST_KEYS)}):", re.MULTILINE)


def detect_format(content: str) -> str:
    stripped = content.strip()
    if link.is_link(stripped):
        return "link"
    if stripped[:1] in "{[" or _STRUCTURED_KEY.search(stripped):
        try:
            if structured.is_structured(structured.load(stripped)):
                return "yaml"
        except structured.StructuredError:
            pass
        if structured.json_document(stripped) is not None:
            return "json"
    if markdown.has_items(stripped) or markdown.title_of(stripped):
        return "markdown"
    return "text"


async def _parse_as(content: str, fmt: str, use_ai: bool) -> ParsedRunbook | None:
    if fmt == "yaml":
        try:
            return structured.parse(content)
        except structured.StructuredError:
            return None
    if fmt == "markdown":
        return markdown.parse(content)
    if fmt == "link":
        return link.parse(content)
    if fmt == "json":
        return structured.raw(content)
    return await text.parse(content, use_ai=use_ai)


async def parse_runbook(content: str, fmt: str | None = "auto", *, use_ai: bool = True) -> ParsedRunbook:
    if not content.strip():
        raise AppError(422, "validation_error", "content: must not be empty")
    if len(content.encode()) > MAX_CONTENT_BYTES:
        raise AppError(413, "payload_too_large", f"content: longer than {MAX_CONTENT_BYTES} bytes")
    if fmt not in (None, "auto", *FORMATS):
        raise AppError(422, "validation_error", f"format: must be one of auto, {', '.join(FORMATS)}")
    requested = fmt if fmt in FORMATS else None
    for candidate in dict.fromkeys([requested, detect_format(content), "text"]):
        if candidate is None:
            continue
        parsed = await _parse_as(content, candidate, use_ai)
        if parsed is not None:
            return parsed
    raise AppError(422, "validation_error", "content: could not be parsed")
