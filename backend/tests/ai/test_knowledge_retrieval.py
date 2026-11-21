"""Chunking, indexing with real embeddings, hybrid retrieval (vector + FTS, RRF) and scope isolation."""

from __future__ import annotations

import httpx
import pytest

from app.ai.embeddings import DIMENSIONS
from app.ai.retrieval import Hit, KnowledgeScope, exact_memory, fts_query, hybrid_search, rrf_merge
from app.core.errors import AppError
from app.core.scope import load_scope
from app.knowledge.chunking import chunk_text
from app.knowledge.fetch import check_url, fetch_url, html_to_text
from app.knowledge.indexer import index_document, index_resolution, on_resolved, sweep_unindexed
from tests.ai.conftest import add_resolution
from tests.api.alerts_helpers import domain, make_alert, team, user


def test_chunking_sizes_overlap_and_boundaries():
    paragraph = "Settlement engine retries failed workflows after a gateway timeout. " * 4
    text = "\n\n".join(paragraph for _ in range(12))
    chunks = chunk_text(text)
    assert len(chunks) > 3 and all(len(c) <= 800 for c in chunks)
    assert chunks[1][:40] in chunks[0]  # 100 characters of overlap
    assert chunk_text("   ") == [] and chunk_text("short") == ["short"]
    assert chunk_text("a\x00b\r\nc") == ["ab\nc"]
    assert len(chunk_text("x" * 5000)) >= 6  # no break points: hard cut


def test_rrf_merges_ranks_and_keeps_the_best_chunk_per_document():
    def hit(n: int, doc: int) -> Hit:
        from uuid import UUID

        return Hit(UUID(int=n), UUID(int=doc), "upload", f"d{doc}", None, None, "t")

    a, b, c = hit(1, 1), hit(2, 2), hit(3, 1)
    merged = rrf_merge([a, b], [hit(2, 2), c])
    assert [h.doc_id.int for h in merged] == [2, 1]  # chunk 2 is ranked by both lists
    assert merged[0].ranks == {"vector": 2, "fts": 1}
    assert fts_query("Timeout in ledger-sync (503)! timeout") == "timeout | ledger | sync | 503"


async def test_upload_creates_chunks_with_embeddings(pool):
    async with pool.acquire() as conn:
        doc = await index_document(
            conn,
            domain_id=domain("payments"),
            team_id=None,
            kind="upload",
            title="Card gateway wiki",
            text=("Card gateway timeouts are caused by the acquirer failover. " * 30),
        )
        rows = await conn.fetch(
            "SELECT embedding, tsv IS NOT NULL AS has_tsv FROM knowledge_chunks WHERE doc_id = $1", doc["id"]
        )
    assert len(rows) >= 2 and doc["indexed_at"] is not None
    assert all(len(r["embedding"].to_list()) == DIMENSIONS and r["has_tsv"] for r in rows)


async def test_hybrid_retrieval_finds_the_similar_incident_by_vector_and_fts(pool):
    old = await make_alert(
        pool,
        title="Ledger reconciliation lag in settlement engine",
        team_slug="team-settlements",
        domain_slug="settlements",
        status="resolved",
        with_refs=False,
    )
    resolution_id = await add_resolution(
        pool, old, "Restarted the ledger-sync consumer; reconciliation lag cleared within two minutes."
    )
    other = await make_alert(
        pool,
        title="Unrelated disk usage warning on batch host",
        team_slug="team-settlements",
        domain_slug="settlements",
        status="resolved",
        with_refs=False,
    )
    await add_resolution(pool, other, "Rotated the archived batch files and expanded the volume.")
    async with pool.acquire() as conn:
        assert await index_resolution(conn, resolution_id)
        assert not await index_resolution(conn, resolution_id)  # idempotent
        assert await sweep_unindexed(conn) >= 1  # picks up `other`
        ks = KnowledgeScope.of_alert(domain("settlements"), team("team-settlements"))
        hits = await hybrid_search(conn, "settlement engine ledger reconciliation lag", ks)
    assert hits and hits[0].alert_id == old["id"] and hits[0].kind == "resolution_note"
    assert set(hits[0].ranks) == {"vector", "fts"} and hits[0].similarity > 0.5

    async with pool.acquire() as conn:
        excluded = await hybrid_search(conn, "ledger reconciliation lag", ks, exclude_alert=old["id"])
        assert all(h.alert_id != old["id"] for h in excluded)
        assert await hybrid_search(conn, "   ", ks) == []


async def test_vector_and_fts_results_never_cross_the_scope(seeded):
    pool = seeded
    async with pool.acquire() as conn:
        await index_document(
            conn,
            domain_id=domain("payments"),
            team_id=None,
            kind="upload",
            title="Payments secret playbook",
            text="Zephyrquartz chargeback fraud playbook: freeze the merchant payout and call the acquirer.",
        )
        await index_document(
            conn,
            domain_id=domain("payments"),
            team_id=team("team-payments"),
            kind="upload",
            title="Team only note",
            text="Zephyrquartz team-internal escalation phone tree for chargebacks.",
        )
        query = "zephyrquartz chargeback fraud playbook"
        payments_scope = KnowledgeScope.of_user(await load_scope(conn, user("anna@demo")))
        settlements_scope = KnowledgeScope.of_user(await load_scope(conn, user("piotr@demo")))
        master_scope = KnowledgeScope.of_user(await load_scope(conn, user("master@demo")))
        mine = await hybrid_search(conn, query, payments_scope)
        theirs = await hybrid_search(conn, query, settlements_scope)
        everything = await hybrid_search(conn, query, master_scope)
        sibling_team = await hybrid_search(
            conn, query, KnowledgeScope.of_alert(domain("payments"), team("team-security"))
        )
    assert {h.title for h in mine} >= {"Payments secret playbook", "Team only note"}
    assert not any("Zephyrquartz" in h.text for h in theirs)
    assert {h.title for h in everything} >= {"Payments secret playbook"}
    titles = {h.title for h in sibling_team}
    assert "Payments secret playbook" in titles and "Team only note" not in titles  # team doc stays private


async def test_exact_memory_is_domain_scoped_and_excludes_the_alert_itself(pool):
    first = await make_alert(pool, knowledge_key="kk-exact-1", status="resolved")
    await add_resolution(pool, first, "Failed over the card gateway.")
    second = await make_alert(pool, knowledge_key="kk-exact-1")
    elsewhere = await make_alert(
        pool, knowledge_key="kk-exact-1", team_slug="team-settlements", domain_slug="settlements"
    )
    await add_resolution(pool, elsewhere, "Different domain note.")
    async with pool.acquire() as conn:
        found = await exact_memory(conn, second["domain_id"], "kk-exact-1")
        assert [f["resolutionNote"] for f in found] == ["Failed over the card gateway."]
        assert found[0]["alertId"] == str(first["id"])
        assert await exact_memory(conn, second["domain_id"], "kk-exact-1", exclude_alert=first["id"]) == []
        assert await exact_memory(conn, second["domain_id"], None) == []
        assert await on_resolved(conn, first["id"]) == 1


def test_fetch_guards_and_html_conversion():
    with pytest.raises(AppError):
        check_url("file:///etc/passwd")
    with pytest.raises(AppError):
        check_url("http://169.254.169.254/latest/meta-data")
    check_url("https://wiki.example.com/page")
    title, text = html_to_text(
        "<html><head><title>Runbook</title><style>x{}</style></head>"
        "<body><h1>Steps</h1><p>Restart it.</p><script>evil()</script></body></html>"
    )
    assert title == "Runbook" and "Restart it." in text and "evil" not in text


async def test_fetch_url_limits_and_content_types():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/md":
            return httpx.Response(200, text="# Doc\nbody", headers={"content-type": "text/markdown"})
        if path == "/html":
            return httpx.Response(
                200, text="<title>T</title><p>hi</p>", headers={"content-type": "text/html"}
            )
        if path == "/big":
            return httpx.Response(200, content=b"x" * 1_100_000, headers={"content-type": "text/plain"})
        if path == "/bin":
            return httpx.Response(200, content=b"\x00", headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_url("https://x.example/md", client=client) == ("md", "# Doc\nbody")
        assert await fetch_url("https://x.example/html", client=client) == ("T", "hi")
        for path, code in (("/big", "too_large"), ("/bin", "unsupported_type"), ("/none", "fetch_failed")):
            with pytest.raises(AppError) as err:
                await fetch_url(f"https://x.example{path}", client=client)
            assert err.value.code == code
