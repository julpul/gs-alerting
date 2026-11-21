"""Helpers shared by the org / history / knowledge seed loaders (not a loader: the name has no number)."""

from __future__ import annotations

import inspect
import logging
import os
import re
from datetime import timedelta
from typing import Any

import asyncpg

log = logging.getLogger("seed")

PROFILE_ENV = "SEED_PROFILE"
SKIP_EMBEDDINGS_ENV = "SEED_SKIP_EMBEDDINGS"
EMBED_BATCH = 32
EMBED_DIM = 384

_OFFSET = re.compile(r"^(?P<neg>-)?(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$")


def profile() -> str:
    """`full` (default): the whole Vistula Capital org, history and knowledge. `core`: only the seven original
    demo identities and no history / knowledge; the test suite runs with it so older tests keep a tiny org."""
    return os.environ.get(PROFILE_ENV, "full").strip().lower() or "full"


def parse_offset(text: str) -> timedelta:
    """`-12d3h` / `52m` / `1h35m` / `0s` -> timedelta (negative when it starts with `-`)."""
    match = _OFFSET.match(str(text).strip())
    if match is None or not any(match.group(g) for g in "dhms"):
        raise ValueError(f"bad time offset {text!r}")
    delta = timedelta(
        days=int(match["d"] or 0),
        hours=int(match["h"] or 0),
        minutes=int(match["m"] or 0),
        seconds=int(match["s"] or 0),
    )
    return -delta if match["neg"] else delta


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Windows of about `size` characters overlapping by `overlap`, cut at a paragraph/line/word boundary."""
    text = text.strip()
    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        end = min(pos + size, len(text))
        if end < len(text):
            for sep in ("\n\n", "\n", ". ", " "):
                cut = text.rfind(sep, pos + size // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        piece = text[pos:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        pos = max(end - overlap, pos + 1)
    return chunks


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embeddings from `app.ai.embeddings.embed_texts` (P3-AI), or None when skipped / not available.

    None leaves `knowledge_chunks.embedding` NULL; the next seed run (or P3-AI's backfill) fills it in."""
    if os.environ.get(SKIP_EMBEDDINGS_ENV, "").lower() in ("1", "true", "yes"):
        return None
    try:
        from app.ai.embeddings import embed_texts as embed
    except ImportError:
        log.info("app.ai.embeddings not available; chunks stay without embeddings")
        return None
    try:
        result = embed(texts)
        vectors = await result if inspect.isawaitable(result) else result
        return [[float(x) for x in v] for v in vectors]
    except Exception:
        log.warning("embedding failed; chunks stay without embeddings", exc_info=True)
        return None


async def fill_missing_embeddings(conn: asyncpg.Connection) -> int:
    """Embed every chunk whose embedding is NULL. Returns how many were filled (0 when skipped)."""
    filled = 0
    while True:
        rows = await conn.fetch(
            "SELECT id, text FROM knowledge_chunks WHERE embedding IS NULL ORDER BY id LIMIT $1", EMBED_BATCH
        )
        if not rows:
            return filled
        vectors = await embed_texts([r["text"] for r in rows])
        if vectors is None or len(vectors) != len(rows):
            return filled
        if any(len(v) != EMBED_DIM for v in vectors):
            log.warning("embedding model does not return %d dimensions; skipping", EMBED_DIM)
            return filled
        for row, vector in zip(rows, vectors, strict=True):
            await conn.execute("UPDATE knowledge_chunks SET embedding = $2 WHERE id = $1", row["id"], vector)
        filled += len(rows)


def replace_placeholders(value: Any, opened_iso: str, opened_epoch_ms: int) -> Any:
    """`"@openedAt"` -> ISO timestamp, `"@openedAt:epoch_ms"` -> integer, recursively."""
    if value == "@openedAt":
        return opened_iso
    if value == "@openedAt:epoch_ms":
        return opened_epoch_ms
    if isinstance(value, dict):
        return {k: replace_placeholders(v, opened_iso, opened_epoch_ms) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_placeholders(v, opened_iso, opened_epoch_ms) for v in value]
    return value
