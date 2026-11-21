"""Text chunking for the knowledge base: ~800 characters with 100 characters of overlap (spec 12)."""

from __future__ import annotations

import re

CHUNK_SIZE = 800
OVERLAP = 100
MAX_CHUNKS = 2000
_BREAKS = ("\n\n", "\n", ". ", " ")


def _cut(text: str, start: int, size: int) -> int:
    """End index of the chunk starting at `start`: prefers a paragraph/sentence/word boundary."""
    end = min(start + size, len(text))
    if end == len(text):
        return end
    floor = start + size // 2
    for sep in _BREAKS:
        pos = text.rfind(sep, floor, end)
        if pos != -1:
            return pos + len(sep)
    return end


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\x00", "").replace("\r\n", "\n")).strip()
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < MAX_CHUNKS:
        end = _cut(text, start, size)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
