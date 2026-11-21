"""Plain-text log bodies -> raw events (one per line), so a `text/plain` webhook or a tailed log works with
zero configuration.

Each non-empty line becomes `{"message": <line>}` plus what ordinary log lines carry:

* a leading timestamp (ISO-8601 `2026-11-20T14:05:09.120Z`, `2026-11-20 14:05:09,120`, or syslog
  `Nov 20 14:05:09`, optionally after a `<13>` syslog priority) -> `timestamp` (the text as written; the
  mapping parses ISO, a syslog stamp has no year and falls back to the time received); after a syslog
  stamp also `host`, `app`, `pid`,
* a level word right after it (`ERROR`, `WARN`, `[info]`, `error:` ...; a lower-case word only in brackets
  or before a colon, so "alert fired" is not a level; else the syslog priority's severity) -> `level`,
* `key=value` and `key="quoted value"` pairs -> fields (numbers become numbers).

A line that is indented or looks like a stack-trace continuation (`at ...`, `Caused by:`, `Traceback`,
`File "..."`, `... 12 more`) is attached to the previous line's event as `stack`, not a new event.
Parsing is regex-only over untrusted text: nothing is evaluated.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LINE = 8192
MAX_PAIRS = 50

_PRIORITY = re.compile(r"^<(?P<pri>\d{1,3})>\s*")
_SYSLOG_TAG = re.compile(r"^(?P<host>[\w.\-]+)\s+(?P<app>[\w.\-/]+)(?:\[(?P<pid>\d+)\])?:\s+")
SYSLOG_LEVELS = ("EMERG", "ALERT", "CRIT", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG")
_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?)(?:\s+|$)"
)
_SYSLOG = re.compile(
    r"^(?P<ts>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})(?:\s+|$)"
)
_LEVEL = re.compile(
    r"^\[?(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|SEVERE|CRIT|CRITICAL|FATAL|ALERT|EMERG)"
    r"\]?:?(?:\s+|$)",
    re.IGNORECASE,
)
_PAIR = re.compile(
    r'(?<![\w.\-])(?P<key>[A-Za-z_][\w.\-]{0,63})=(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<bare>[^\s"]*))'
)
_CONTINUATION = re.compile(r"^(?:\s+\S|at\s|Caused by:|Traceback |File \"|\.\.\. \d+ more)")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_RESERVED = {"message", "stack"}


def _scalar(text: str) -> Any:
    if _NUMBER.match(text) and len(text) <= 18:
        return float(text) if "." in text else int(text)
    return text


def parse_line(line: str) -> dict[str, Any]:
    """One log line -> a raw event."""
    line = line.strip()[:MAX_LINE]
    event: dict[str, Any] = {"message": line}
    rest = line
    priority = _PRIORITY.match(rest)
    if priority:
        rest = rest[priority.end() :]
    stamp = _ISO.match(rest) or _SYSLOG.match(rest)
    if stamp:
        event["timestamp"] = stamp.group("ts")
        rest = rest[stamp.end() :]
        tag = _SYSLOG_TAG.match(rest) if stamp.re is _SYSLOG else None
        if tag:
            event.update({k: v for k, v in tag.groupdict().items() if v})
            rest = rest[tag.end() :]
    level = _LEVEL.match(rest)
    if level and (level.group("level").isupper() or level.group(0).rstrip()[-1] in "]:"):
        event["level"] = level.group("level").upper()
    elif priority and int(priority.group("pri")) < 192:
        event["level"] = SYSLOG_LEVELS[int(priority.group("pri")) % 8]
    for count, pair in enumerate(_PAIR.finditer(line)):
        if count >= MAX_PAIRS:
            break
        key = pair.group("key")
        if key in _RESERVED or key in event:
            continue
        quoted = pair.group("quoted")
        event[key] = quoted.replace('\\"', '"') if quoted is not None else _scalar(pair.group("bare"))
    return event


def parse_text(text: str) -> list[dict[str, Any]]:
    """A text body -> one event per non-empty line (stack-trace continuations attach to their line)."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if events and _CONTINUATION.match(line):
            previous = events[-1]
            stack = previous.get("stack")
            previous["stack"] = f"{stack}\n{line.rstrip()}" if stack else line.rstrip()
            continue
        events.append(parse_line(line))
    return events


def is_json_content_type(content_type: str | None) -> bool:
    """`application/json` / `application/*+json`: such a body must be JSON (no text fallback)."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")
