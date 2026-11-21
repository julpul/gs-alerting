"""Fetch a document from a URL for the knowledge base (1 MB cap, http/https only)."""

from __future__ import annotations

import ipaddress
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from app.core.errors import AppError

MAX_BYTES = 1_000_000
TIMEOUT_S = 10.0
BLOCKED_HOSTS = {"metadata.google.internal", "metadata"}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, text)."""
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts))
    return parser.title.strip(), re.sub(r"[ \t]+", " ", text).strip()


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AppError(422, "invalid_url", "only http(s) URLs can be imported")
    host = parts.hostname.lower()
    blocked = host in BLOCKED_HOSTS
    try:
        ip = ipaddress.ip_address(host)
        blocked = blocked or ip.is_link_local or ip.is_unspecified or ip.is_multicast
    except ValueError:
        pass
    if blocked:
        raise AppError(422, "invalid_url", "this address cannot be imported")


async def fetch_url(url: str, *, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """Returns (title, text) of the document at `url`."""
    check_url(url)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        async with client.stream(
            "GET", url, headers={"Accept": "text/html,text/markdown,text/plain"}
        ) as resp:
            if resp.status_code >= 400:
                raise AppError(422, "fetch_failed", f"the URL answered HTTP {resp.status_code}")
            body = bytearray()
            async for part in resp.aiter_bytes():
                body.extend(part)
                if len(body) > MAX_BYTES:
                    raise AppError(422, "too_large", "the document is larger than 1 MB")
            content_type = resp.headers.get("content-type", "text/plain").split(";")[0].strip().lower()
    except httpx.HTTPError as exc:
        raise AppError(422, "fetch_failed", f"could not fetch the URL: {type(exc).__name__}") from exc
    finally:
        if owned:
            await client.aclose()
    text = bytes(body).decode("utf-8", errors="replace")
    if content_type in ("text/html", "application/xhtml+xml"):
        title, text = html_to_text(text)
    elif content_type.startswith("text/") or content_type in ("application/json", "application/markdown"):
        title = ""
    else:
        raise AppError(422, "unsupported_type", f"unsupported content type {content_type}")
    return title or urlsplit(url).path.rsplit("/", 1)[-1] or urlsplit(url).hostname or url, text
