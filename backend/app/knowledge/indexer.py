"""Knowledge indexing: documents and resolution notes -> chunks -> local embeddings -> `knowledge_chunks`."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg

from app.ai.embeddings import embed_texts
from app.core.ids import uuid7
from app.knowledge.chunking import chunk_text

log = logging.getLogger(__name__)

DOC_COLUMNS = "id, title, kind, team_id, indexed_at"


def doc_wire(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "kind": row["kind"],
        "teamId": str(row["team_id"]) if row["team_id"] else None,
        "indexedAt": row["indexed_at"],
    }


async def index_document(
    conn: asyncpg.Connection,
    *,
    domain_id: UUID,
    team_id: UUID | None,
    kind: str,
    title: str,
    text: str,
    url: str | None = None,
    alert_id: UUID | None = None,
    resolution_id: UUID | None = None,
) -> asyncpg.Record | None:
    """Chunk, embed and store a document; None when its resolution was indexed already."""
    chunks = chunk_text(text)
    vectors = await embed_texts(chunks)
    doc_id = uuid7()
    async with conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO knowledge_docs "
            "(id, domain_id, team_id, kind, title, url, alert_id, resolution_id, indexed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now()) ON CONFLICT DO NOTHING "
            f"RETURNING {DOC_COLUMNS}",
            doc_id,
            domain_id,
            team_id,
            kind,
            title[:300],
            url,
            alert_id,
            resolution_id,
        )
        if row is None:
            return None
        await conn.executemany(
            "INSERT INTO knowledge_chunks (id, doc_id, domain_id, team_id, text, embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6::vector)",
            [
                (uuid7(), doc_id, domain_id, team_id, chunk, vec)
                for chunk, vec in zip(chunks, vectors, strict=True)
            ],
        )
    return row


async def index_resolution(conn: asyncpg.Connection, resolution_id: UUID) -> bool:
    """Embed one resolution note (with the alert title) as a `resolution_note` document."""
    res = await conn.fetchrow(
        "SELECT r.id, r.alert_id, r.domain_id, r.team_id, r.note, a.title "
        "FROM resolutions r JOIN alerts a ON a.id = r.alert_id WHERE r.id = $1",
        resolution_id,
    )
    if res is None:
        return False
    row = await index_document(
        conn,
        domain_id=res["domain_id"],
        team_id=None,  # incident memory is domain-wide, like the L1 lookup
        kind="resolution_note",
        title=res["title"],
        text=f"{res['title']}\nResolution: {res['note']}",
        alert_id=res["alert_id"],
        resolution_id=res["id"],
    )
    return row is not None


async def on_resolved(conn: asyncpg.Connection, alert_id: UUID) -> int:
    """Index the resolution notes of an alert that was just resolved; returns how many were indexed."""
    ids = await conn.fetch("SELECT id FROM resolutions WHERE alert_id = $1", alert_id)
    done = 0
    for row in ids:
        done += await index_resolution(conn, row["id"])
    return done


async def sweep_unindexed(conn: asyncpg.Connection, limit: int = 50) -> int:
    """Index resolutions that have no knowledge doc yet (seeded history, missed tasks)."""
    rows = await conn.fetch(
        "SELECT r.id FROM resolutions r JOIN alerts a ON a.id = r.alert_id "
        "LEFT JOIN knowledge_docs d ON d.resolution_id = r.id WHERE d.id IS NULL "
        "ORDER BY r.created_at LIMIT $1",
        limit,
    )
    done = 0
    for row in rows:
        try:
            done += await index_resolution(conn, row["id"])
        except Exception:
            log.warning("could not index resolution %s", row["id"], exc_info=True)
    return done
