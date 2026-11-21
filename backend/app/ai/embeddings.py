"""Local embeddings (fastembed, `BAAI/bge-small-en-v1.5`, 384 dims): loaded lazily once per process and run
in a worker thread so the event loop never blocks."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

DIMENSIONS = 384
_model: Any = None
_lock = threading.Lock()


def _cache_dir() -> str:
    explicit = os.environ.get("FASTEMBED_CACHE_PATH")
    return explicit or str(Path.home() / ".cache" / "gs-fastembed")


def _load() -> Any:
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding

            log.info("loading embedding model %s", get_settings().embeddings_model)
            _model = TextEmbedding(get_settings().embeddings_model, cache_dir=_cache_dir())
        return _model


def _embed_sync(texts: list[str]) -> list[list[float]]:
    return [[float(x) for x in vec] for vec in _load().embed(texts)]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return await asyncio.to_thread(_embed_sync, texts)


async def warm_up() -> None:
    """Load the model in a thread (called at worker start so the first suggestion is not slow)."""
    try:
        await asyncio.to_thread(_load)
    except Exception:
        log.warning("could not preload the embedding model", exc_info=True)


async def embed_one(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
