"""A link to a wiki / Confluence page: one "Open runbook" step carrying the URL."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from app.runbooks.model import ParsedRunbook, finalize, make_step

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
MAX_URL = 2000


def first_url(content: str) -> str | None:
    m = _URL.search(content.strip())
    return m.group(0).rstrip(".,)") if m and len(m.group(0)) <= MAX_URL else None


def is_link(content: str) -> bool:
    stripped = content.strip()
    url = first_url(stripped)
    return url is not None and stripped.startswith(url) and len(stripped.split()) == 1


def parse(content: str) -> ParsedRunbook | None:
    url = first_url(content)
    if url is None:
        return None
    parts = urlsplit(url)
    leaf = (
        unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
        .replace("-", " ")
        .replace("_", " ")
        .replace("+", " ")
    )
    name = leaf.strip().title() or parts.hostname or "Linked runbook"
    step = make_step(text=f"Open runbook: {url}", kind="text", url=url, parameters={"url": url})
    return ParsedRunbook(name=name, format="link", steps=finalize([step]))
