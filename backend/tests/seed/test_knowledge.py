"""Knowledge documents and chunks; embeddings via a stub of `app.ai.embeddings` (P3-AI)."""

import sys
import types
from importlib import import_module
from pathlib import Path

from app.core.ids import stable_id

loader = import_module("seed.loaders.95_knowledge")
support = import_module("seed.loaders._support")


async def test_six_documents_with_chunks_and_scopes(full_pool):
    docs = await full_pool.fetch(
        "SELECT d.id, d.title, d.kind, d.team_id, d.domain_id, count(c.id) AS chunks "
        "FROM knowledge_docs d JOIN knowledge_chunks c ON c.doc_id = d.id "
        "WHERE d.kind <> 'resolution_note' GROUP BY d.id"
    )
    assert len(docs) == 6
    assert all(d["chunks"] >= 2 and d["kind"] == "upload" for d in docs)
    by_title = {d["title"]: d for d in docs}
    policy = by_title["Policy: trade limits and rejected trades"]
    assert policy["team_id"] == stable_id("team", "team-rates")
    assert policy["domain_id"] == stable_id("domain", "markets")
    assert policy["id"] == stable_id("knowledge_doc", "policy-trade-limits")


async def test_chunk_sizes_follow_the_spec(full_pool):
    lengths = [
        r["n"]
        for r in await full_pool.fetch(
            "SELECT length(c.text) AS n FROM knowledge_chunks c JOIN knowledge_docs d ON d.id = c.doc_id "
            "WHERE d.kind <> 'resolution_note'"
        )
    ]
    assert max(lengths) <= 800 and sum(lengths) / len(lengths) > 300


async def test_chunks_are_full_text_searchable(full_pool):
    hits = await full_pool.fetch(
        "SELECT d.title FROM knowledge_chunks c JOIN knowledge_docs d ON d.id = c.doc_id "
        "WHERE c.tsv @@ plainto_tsquery('simple', 'noisy neighbour')"
    )
    assert any("order-settlement" in h["title"] for h in hits)


async def test_embeddings_are_backfilled_when_the_model_is_available(full_pool, monkeypatch):
    calls: list[int] = []

    def embed_texts(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return [[0.1] * 384 for _ in texts]

    module = types.ModuleType("app.ai.embeddings")
    module.embed_texts = embed_texts
    monkeypatch.setitem(sys.modules, "app.ai.embeddings", module)
    monkeypatch.delenv("SEED_SKIP_EMBEDDINGS")
    total = await full_pool.fetchval("SELECT count(*) FROM knowledge_chunks")
    assert await full_pool.fetchval("SELECT count(*) FROM knowledge_chunks WHERE embedding IS NULL") == total

    async with full_pool.acquire() as conn:
        await loader.load(conn, {})
        assert await conn.fetchval("SELECT count(*) FROM knowledge_chunks WHERE embedding IS NULL") == 0
        assert sum(calls) == total
        await loader.load(conn, {})  # nothing left to embed
    assert sum(calls) == total


async def test_wrong_dimension_or_failure_leaves_embeddings_null(full_pool, monkeypatch):
    module = types.ModuleType("app.ai.embeddings")
    monkeypatch.setitem(sys.modules, "app.ai.embeddings", module)
    monkeypatch.delenv("SEED_SKIP_EMBEDDINGS")
    async with full_pool.acquire() as conn:
        await conn.execute("UPDATE knowledge_chunks SET embedding = NULL")
        assert await support.fill_missing_embeddings(conn) == 0  # module has no embed_texts -> ImportError

        async def boom(texts):
            raise RuntimeError("model down")

        module.embed_texts = boom
        assert await support.fill_missing_embeddings(conn) == 0
        module.embed_texts = lambda texts: [[0.0] * 3 for _ in texts]
        assert await support.fill_missing_embeddings(conn) == 0
        assert await conn.fetchval("SELECT count(*) FROM knowledge_chunks WHERE embedding IS NOT NULL") == 0


async def test_edited_chunk_loses_its_stale_embedding(full_pool):
    chunk = stable_id("knowledge_chunk", "policy-trade-limits:0")
    async with full_pool.acquire() as conn:
        await conn.execute("UPDATE knowledge_chunks SET embedding = $2 WHERE id = $1", chunk, [0.2] * 384)
        await loader.load(conn, {})
        assert await conn.fetchval("SELECT embedding IS NOT NULL FROM knowledge_chunks WHERE id = $1", chunk)
        await conn.execute("UPDATE knowledge_chunks SET text = 'old text' WHERE id = $1", chunk)
        await loader.load(conn, {})
        assert await conn.fetchval("SELECT embedding IS NULL FROM knowledge_chunks WHERE id = $1", chunk)
        assert "old text" not in await conn.fetchval("SELECT text FROM knowledge_chunks WHERE id = $1", chunk)


def test_every_markdown_has_front_matter():
    paths = sorted(loader.KNOWLEDGE_DIR.glob("*.md"))
    assert len(paths) == 6
    for path in paths:
        meta, body = loader.parse_document(Path(path))
        assert {"title", "kind", "domain", "team"} <= set(meta) and len(body) > 500
