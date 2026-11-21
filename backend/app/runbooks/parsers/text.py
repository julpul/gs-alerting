"""Free text (copied from a wiki or chat) -> steps.

With an LLM configured and `app.ai.runbook_parse.parse_text` available the AI draft is used
(`parsedBy: ai`); otherwise a deterministic sentence splitter with the same verb heuristics as the Markdown
parser (`parsedBy: heuristic`). Either way the result is a draft for a human to review.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import get_settings
from app.runbooks.actions.registry import get_action
from app.runbooks.model import ParsedRunbook, finalize, make_step
from app.runbooks.parsers.heuristics import is_conditional, step_from_sentence, suggest_action
from app.schemas import api as wire

log = logging.getLogger(__name__)

_SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(#])|\n+|;\s+")
_VERBS = r"wait|call|page|phone|restart|retry|re-?run|roll|fail|check|open|post|escalate|notify|if|then"
_COMMAND = re.compile(
    r"^(?:\$\s+|sudo\s|ssh\s|curl\s|kubectl\s|systemctl\s|docker\s|sleep\s+\d|grep\s|export\s|cd\s)"
)
_CLAUSES = re.compile(rf",\s*(?:and\s+|then\s+)?(?=(?:{_VERBS})\b)", re.IGNORECASE)


def ai_available() -> bool:
    s = get_settings()
    return bool(s.openai_api_key or s.anthropic_api_key or s.openrouter_api_key)


def split_steps(text: str) -> list[str]:
    fragments: list[str] = []
    for sentence in _SENTENCES.split(text):
        sentence = sentence.strip().rstrip(".!?").strip()
        if sentence:
            fragments += [f.strip() for f in _CLAUSES.split(sentence) if f.strip()]
    merged: list[str] = []
    i = 0
    while i < len(fragments):
        frag = fragments[i]
        nxt = fragments[i + 1] if i + 1 < len(fragments) else None
        if nxt and is_conditional(frag) and suggest_action(frag) is None and not is_conditional(nxt):
            merged.append(f"{frag}: {nxt}")
            i += 2
        else:
            merged.append(frag)
            i += 1
    return [f"{m[0].upper()}{m[1:]}" for m in merged]


def looks_like_shell(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if lines[0].startswith("#!"):
        return True
    commands = sum(1 for line in lines if _COMMAND.match(line))
    return commands >= 2 and commands * 2 >= len(lines)


def shell_steps(text: str) -> list[wire.RunbookStep]:
    """A shell script, one step per line, for reading only: commands are never executed. A recognised verb
    (`systemctl restart`) still suggests a registered action; comments become plain instructions."""
    steps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#!"):
            continue
        if line.startswith("#"):
            comment = line.lstrip("#").strip()
            if comment:
                steps.append(step_from_sentence(comment[0].upper() + comment[1:]))
            continue
        steps.append(step_from_sentence(f"Run by hand: `{line.removeprefix('$ ')}`"))
    return steps


def heuristic_parse(text: str) -> ParsedRunbook:
    pieces = shell_steps(text) if looks_like_shell(text) else None
    steps = pieces or [step_from_sentence(s) for s in split_steps(text)]
    if not steps:
        steps = [make_step(text=text.strip()[:2000], parsed_by="heuristic")]
    return ParsedRunbook(name="Runbook", format="text", steps=finalize(steps), parsed_by="heuristic")


def _coerce(item: Any) -> wire.RunbookStep | None:
    data = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
    text = str(data.get("text") or "").strip()
    if not text:
        return None
    action = data.get("action")
    if action and get_action(str(action)) is None:
        action = None
    return make_step(
        text=text,
        kind="action" if action else (data.get("kind") if data.get("kind") in ("text", "check") else "text"),
        action=action,
        parameters=data.get("parameters") or {},
        approval="required" if str(data.get("approval")) == "required" else "none",
        suggested=bool(action),
        parsed_by="ai",
    )


async def _ai_parse(text: str) -> ParsedRunbook | None:
    try:
        from app.ai.runbook_parse import parse_text
    except ImportError:
        return None
    try:
        drafted = await parse_text(text)
    except Exception:
        log.warning("AI runbook parse failed; using the deterministic splitter", exc_info=True)
        return None
    steps = [s for s in (_coerce(i) for i in drafted or []) if s is not None]
    if not steps:
        return None
    return ParsedRunbook(name="Runbook", format="text", steps=finalize(steps), parsed_by="ai")


async def parse(text: str, *, use_ai: bool = True) -> ParsedRunbook:
    if use_ai and ai_available():
        parsed = await _ai_parse(text)
        if parsed is not None:
            return parsed
    return heuristic_parse(text)
