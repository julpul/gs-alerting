"""Seed the knowledge base: markdown documents (post-mortems, wiki pages, a runbook, a policy) as chunks.

Chunks are ~800 characters with 100 overlap. Embeddings come from `app.ai.embeddings.embed_texts` when it is
importable and `SEED_SKIP_EMBEDDINGS` is not set; otherwise `embedding` stays NULL and the next seed run (or
the AI knowledge backfill) fills it. The same pass also embeds the resolution-note chunks of `90_history`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
import yaml

from app.core.ids import stable_id
from seed.loaders._support import chunk_text, fill_missing_embeddings, profile

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "knowledge"
# `knowledge_docs.kind` is the contract enum (upload | link | jira | confluence | resolution_note); the
# front-matter kind (postmortem, wiki, runbook, policy) only describes the content.
DOC_KIND = "upload"


def parse_document(path: Path) -> tuple[dict[str, Any], str]:
    """Markdown with a `---` YAML front matter -> (meta, body)."""
    text = path.read_text()
    if text.startswith("---\n"):
        _, front, body = text.split("---\n", 2)
        return yaml.safe_load(front) or {}, body.strip()
    return {}, text.strip()


async def _load_document(conn: asyncpg.Connection, path: Path) -> None:
    meta, body = parse_document(path)
    doc_id = stable_id("knowledge_doc", path.stem)
    domain_id = stable_id("domain", meta["domain"])
    team_id = stable_id("team", meta["team"]) if meta.get("team") else None
    await conn.execute(
        "INSERT INTO knowledge_docs (id, domain_id, team_id, kind, title) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (id) DO UPDATE SET domain_id = EXCLUDED.domain_id, team_id = EXCLUDED.team_id, "
        "kind = EXCLUDED.kind, title = EXCLUDED.title",
        doc_id,
        domain_id,
        team_id,
        DOC_KIND,
        meta["title"],
    )
    chunks = chunk_text(body)
    for index, text in enumerate(chunks):
        # Unchanged chunks keep their embedding; edited ones are re-embedded.
        await conn.execute(
            "INSERT INTO knowledge_chunks (id, doc_id, domain_id, team_id, text) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (id) DO UPDATE SET domain_id = EXCLUDED.domain_id, team_id = EXCLUDED.team_id, "
            "embedding = CASE WHEN knowledge_chunks.text = EXCLUDED.text "
            "THEN knowledge_chunks.embedding END, text = EXCLUDED.text",
            stable_id("knowledge_chunk", f"{path.stem}:{index}"),
            doc_id,
            domain_id,
            team_id,
            text,
        )
    await conn.execute(
        "DELETE FROM knowledge_chunks WHERE doc_id = $1 AND NOT (id = ANY($2))",
        doc_id,
        [stable_id("knowledge_chunk", f"{path.stem}:{i}") for i in range(len(chunks))],
    )


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    if profile() == "core":
        return
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        await _load_document(conn, path)
    await fill_missing_embeddings(conn)
