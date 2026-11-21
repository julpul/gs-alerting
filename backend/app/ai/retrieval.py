"""Memory lookups for suggestions (spec 13.2): L1 exact resolutions, L2 hybrid retrieval (vector + FTS, RRF).

Every query is filtered by a `KnowledgeScope` (domain-wide docs of the scope's domains, plus team docs of the
scope's teams); vector and full-text results never cross it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from app.ai.embeddings import embed_one
from app.core.scope import Scope

TOP_K = 20
RESULT_K = 5
RRF_K = 60
MAX_QUERY_TERMS = 24
_TERM = re.compile(r"[A-Za-z0-9_]{3,}")


@dataclass(frozen=True)
class KnowledgeScope:
    everything: bool = False
    domain_ids: tuple[UUID, ...] = ()
    team_ids: tuple[UUID, ...] = ()

    @classmethod
    def of_user(cls, scope: Scope) -> KnowledgeScope:
        return cls(scope.is_master, tuple(scope.domain_ids), tuple(scope.team_ids))

    @classmethod
    def of_alert(cls, domain_id: UUID, team_id: UUID) -> KnowledgeScope:
        return cls(False, (domain_id,), (team_id,))

    def where(self, first_param: int = 1, alias: str = "d") -> tuple[str, list[Any]]:
        if self.everything:
            return "TRUE", []
        sql = (
            f"(({alias}.team_id IS NULL AND {alias}.domain_id = ANY(${first_param})) "
            f"OR {alias}.team_id = ANY(${first_param + 1}))"
        )
        return sql, [list(self.domain_ids), list(self.team_ids)]


@dataclass
class Hit:
    chunk_id: UUID
    doc_id: UUID
    kind: str
    title: str
    url: str | None
    alert_id: UUID | None
    text: str
    similarity: float = 0.0
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)


async def exact_memory(
    conn: asyncpg.Connection,
    domain_id: UUID,
    knowledge_key: str | None,
    *,
    exclude_alert: UUID | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """L1: resolutions with the same knowledge key in the domain, newest first (index lookup, no model)."""
    if not knowledge_key:
        return []
    rows = await conn.fetch(
        "SELECT alert_id, note, created_at FROM resolutions "
        "WHERE domain_id = $1 AND knowledge_key = $2 AND alert_id IS DISTINCT FROM $3 "
        "ORDER BY created_at DESC LIMIT $4",
        domain_id,
        knowledge_key,
        exclude_alert,
        limit,
    )
    return [
        {"alertId": str(r["alert_id"]), "resolvedAt": r["created_at"], "resolutionNote": r["note"]}
        for r in rows
    ]


def fts_query(text: str) -> str:
    """OR-query over the distinct alphanumeric terms of `text` (safe to hand to `to_tsquery`)."""
    terms: list[str] = []
    for term in _TERM.findall(text.lower()):
        if term not in terms:
            terms.append(term)
    return " | ".join(terms[:MAX_QUERY_TERMS])


_SELECT = (
    "SELECT c.id AS chunk_id, c.doc_id, c.text, d.kind, d.title, d.url, d.alert_id"
    "{extra} FROM knowledge_chunks c JOIN knowledge_docs d ON d.id = c.doc_id WHERE {where}"
)


async def _vector_hits(conn: asyncpg.Connection, vec: list[float], ks: KnowledgeScope) -> list[Hit]:
    where, args = ks.where(2)
    sql = (
        _SELECT.format(
            extra=", 1 - (c.embedding <=> $1::vector) AS sim", where=f"c.embedding IS NOT NULL AND {where}"
        )
        + f" ORDER BY c.embedding <=> $1::vector LIMIT {TOP_K}"
    )
    async with conn.transaction():
        # filtered ANN: keep scanning the HNSW index until enough rows pass the scope filter (pgvector >= 0.8)
        await conn.execute("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)")
        rows = await conn.fetch(sql, vec, *args)
    return [_hit(r, float(r["sim"])) for r in rows]


async def _fts_hits(conn: asyncpg.Connection, query: str, ks: KnowledgeScope) -> list[Hit]:
    if not query:
        return []
    where, args = ks.where(2)
    sql = (
        _SELECT.format(extra="", where=f"c.tsv @@ to_tsquery('simple', $1) AND {where}")
        + f" ORDER BY ts_rank_cd(c.tsv, to_tsquery('simple', $1)) DESC LIMIT {TOP_K}"
    )
    return [_hit(r, 0.0) for r in await conn.fetch(sql, query, *args)]


def _hit(row: asyncpg.Record, similarity: float) -> Hit:
    return Hit(
        row["chunk_id"],
        row["doc_id"],
        row["kind"],
        row["title"],
        row["url"],
        row["alert_id"],
        row["text"],
        similarity,
    )


def rrf_merge(vector_hits: list[Hit], fts_hits: list[Hit]) -> list[Hit]:
    """Reciprocal rank fusion of two ranked lists, best chunk per document, highest score first."""
    merged: dict[UUID, Hit] = {}
    for label, hits in (("vector", vector_hits), ("fts", fts_hits)):
        for rank, hit in enumerate(hits, start=1):
            current = merged.setdefault(hit.chunk_id, hit)
            current.ranks[label] = rank
            current.score += 1.0 / (RRF_K + rank)
            current.similarity = max(current.similarity, hit.similarity)
    best: dict[UUID, Hit] = {}
    for hit in sorted(merged.values(), key=lambda h: h.score, reverse=True):
        best.setdefault(hit.doc_id, hit)
    return list(best.values())


async def _fill_similarity(conn: asyncpg.Connection, vec: list[float], hits: list[Hit]) -> None:
    missing = [h for h in hits if h.similarity == 0.0]
    if not missing:
        return
    rows = await conn.fetch(
        "SELECT id, 1 - (embedding <=> $1::vector) AS sim FROM knowledge_chunks WHERE id = ANY($2)",
        vec,
        [h.chunk_id for h in missing],
    )
    sims = {r["id"]: float(r["sim"]) for r in rows if r["sim"] is not None}
    for hit in missing:
        hit.similarity = sims.get(hit.chunk_id, 0.0)


async def hybrid_search(
    conn: asyncpg.Connection,
    query: str,
    ks: KnowledgeScope,
    *,
    limit: int = RESULT_K,
    exclude_alert: UUID | None = None,
) -> list[Hit]:
    """L2: pgvector cosine top 20 + full-text top 20, fused with RRF, top `limit` documents."""
    if not query.strip():
        return []
    vec = await embed_one(query)
    vector_hits = await _vector_hits(conn, vec, ks)
    fts = await _fts_hits(conn, fts_query(query), ks)
    hits = [h for h in rrf_merge(vector_hits, fts) if exclude_alert is None or h.alert_id != exclude_alert]
    hits = hits[:limit]
    await _fill_similarity(conn, vec, hits)
    return hits
