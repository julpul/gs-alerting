"""TEST-SCENARIOS §3 S1-S9 (unseen schemas) through `/v1/ingest/{key}` and the real worker, zero config:
every sample is accepted, no event ends `invalid`, and its fields show up in the field picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.redis import rkey
from app.pipeline.normalize import normalize
from app.repositories.sources import read_recent
from tests.pipeline.conftest import eventually

UNSEEN = Path(__file__).resolve().parents[1] / "fixtures" / "unseen"

# sample -> (events it stands for, field paths the picker must offer)
EXPECTED = {
    "s1_cloudevents": (1, {"data.shortfall", "specversion", "@type"}),
    "s2_alertmanager_batch": (2, {"labels.severity", "annotations.summary", "status", "fingerprint"}),
    "s3_text_log": (1, {"message", "instrument", "latency_ms", "msg", "level"}),
    "s4_github_workflow_run": (1, {"workflow_run.conclusion", "repository.full_name"}),
    "s5_array_legs": (1, {"legs.1.status", "legs.*.status", "tags.0", "eventType"}),
    "s6_epoch_health": (1, {"state", "checked_at", "details.missed_heartbeats"}),
    "s7_syslog_numeric": (1, {"sev", "host", "msg"}),
    "s8_payment_nested": (1, {"body.status.code", "body.originalInstruction.amount.value"}),
    "s9_top_level_array": (3, {"evt", "desk", "pct"}),
}


def _content_type(path: Path) -> str:
    return "text/plain" if path.suffix == ".txt" else "application/json"


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_unseen_sample_is_ingested_and_its_fields_are_discoverable(
    name, client, make_source, worker_env
):
    count, paths = EXPECTED[name]
    path = next(UNSEEN.glob(f"{name}.*"))
    source = await make_source()
    resp = await client.post(
        f"/v1/ingest/{source['key']}",
        content=path.read_bytes(),
        headers={"Content-Type": _content_type(path), "X-GitHub-Event": "workflow_run"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["eventId"]
    if "accepted" in body:
        assert body["accepted"] == count == len(body["eventIds"])

    stats_key = rkey("stats", "source", str(source["id"]))

    async def processed() -> dict[str, str] | None:
        stats = await worker_env.redis.hgetall(stats_key)
        return stats if sum(int(v) for v in stats.values()) >= count else None

    stats = await eventually(processed, timeout=30)
    assert "invalid" not in stats, stats
    recent = await read_recent(worker_env.redis, source["id"], limit=10)
    assert len(recent) == count

    picker = await client.get("/api/fields", params={"source": source["name"]})
    assert picker.status_code == 200, picker.text
    offered = {f["path"] for f in picker.json()}
    assert paths <= offered, paths - offered


async def test_unseen_samples_zero_config_meta(make_source):
    """What the built-in guesses find (no mapping): types, timestamps, severities."""
    source = {"id": "00000000-0000-4000-8000-000000000009", "name": "src", "default_severity": "warning"}
    from app.api.routers.ingest import body_events
    from app.pipeline.envelope import build_envelope
    from app.pipeline.presets import split_events

    def metas(name: str) -> list[dict]:
        path = next(UNSEEN.glob(f"{name}.*"))
        events, single = body_events(path.read_bytes(), _content_type(path))
        raws = [p for e in (events or [single]) for p in split_events(None, e)]
        return [normalize(build_envelope(source_id=source["id"], raw=r, priority=3), source).meta for r in raws]

    cloud = metas("s1_cloudevents")[0]
    assert cloud["type"] == "com.example.margin.call.issued"
    assert cloud["timestamp"] == "2026-11-20T14:02:11.000Z"
    am = metas("s2_alertmanager_batch")
    assert [m["severity"] for m in am] == ["critical", "warning"]
    log = metas("s3_text_log")[0]
    assert (log["severity"], log["timestamp"]) == ("warning", "2026-11-20T14:05:09.120Z")
    assert metas("s5_array_legs")[0]["type"] == "ORDER_BATCH_PARTIALLY_FILLED"
    assert len(metas("s9_top_level_array")) == 3
