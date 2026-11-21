"""Report tests: fake k6 summaries + fake SQL rows -> markdown + PNG, no backend needed.

uv run --with pytest --with matplotlib --with asyncpg --with httpx pytest loadtest/tests -q
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import report

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

# `k6 --summary-export` shape (legacy nested "values" and the flat form must both parse).
K6_FLAT = {
    "metrics": {
        "http_req_duration": {"med": 4.2, "p(95)": 9.0, "p(99)": 18.5, "avg": 5, "max": 60},
        "http_reqs": {"count": 30000, "rate": 100},
        "http_req_failed": {"rate": 0.0, "passes": 0, "fails": 30000},
    }
}
K6_NESTED = {
    "metrics": {
        "http_req_duration": {"type": "trend", "values": {"med": 5.0, "p(95)": 10.0, "p(99)": 20.0}},
        "http_reqs": {"type": "counter", "values": {"count": 12}},
        "http_req_failed": {"type": "rate", "values": {"rate": 0.02}},
    }
}
RUN_T1 = {
    "test": "t1",
    "runId": "ltabc",
    "startedAt": "2026-11-21T11:00:00.000Z",
    "endedAt": "2026-11-21T11:05:00.000Z",
    "durationS": 300,
    "counts": {"accepted": 30000, "rejected": 0, "expectedAlerts": 5000},
    "httpMs": {"p50": 4.0, "p95": 9.0, "p99": 18.0},
}
RUN_T2 = {
    "test": "t2",
    "runId": "ltdef",
    "startedAt": "2026-11-21T11:10:00Z",
    "endedAt": "2026-11-21T11:20:00Z",
    "counts": {"accepted": 900000, "rejected": 10},
    "maxSustainedEvPerSec": 2400,
    "acceptedEvPerSec": 1500,
    "stopReason": "threshold abort",
}


def fake_data(**overrides):
    rng = random.Random(1)
    samples = {
        "ui": [rng.lognormvariate(3.0, 0.5) for _ in range(2000)],
        "email": [rng.lognormvariate(5.0, 0.6) for _ in range(500)],
    }
    data = report.ReportData(
        generated_at=NOW,
        since=NOW - timedelta(hours=1),
        until=NOW,
        api_url="http://localhost:8080",
        k6={"t1": report.parse_k6("t1", K6_FLAT, RUN_T1), "t2": report.parse_k6("t2", K6_NESTED, RUN_T2)},
        delivery_latency=[
            report.Latency("ui", 2000, 20.0, 40.0, 120.0, 400.0),
            report.Latency("email", 500, 150.0, 400.0, 1500.0, 2500.0),
        ],
        delivery_samples=samples,
        alert_latency=report.Latency("alerts", 5000, 12.0, 30.0, 80.0, 200.0),
        prom=report.PromData(
            ingest_rate=[(1000.0 + i, 100.0 + i) for i in range(60)],
            processing_p50_ms=11.0,
            processing_p99_ms=240.0,
            outcomes={"alerted_new": 5000, "unrouted": 24000, "grouped": 1000},
            ingested=30000,
            queue_depth={"events.in": [(1000.0 + i, float(i * 3 % 50)) for i in range(60)]},
        ),
        t5=[
            {"workers": 1, "maxSustainedEvPerSec": 700, "stopReason": "backlog"},
            {"workers": 2, "maxSustainedEvPerSec": 1300, "stopReason": "backlog"},
            {"workers": 4, "maxSustainedEvPerSec": 2400, "stopReason": "backlog"},
        ],
        t4_reconcile="LOST = 0",
    )
    for key, value in overrides.items():
        setattr(data, key, value)
    return data


def test_parse_k6_flat_and_nested():
    flat = report.parse_k6("t1", K6_FLAT, RUN_T1)
    assert (flat.http_p50, flat.http_p99, flat.requests, flat.accepted) == (4.2, 18.5, 30000, 30000)
    assert flat.run_id == "ltabc" and flat.started_at.startswith("2026-11-21T11:00")
    nested = report.parse_k6("t2", K6_NESTED, RUN_T2)
    assert (nested.http_p50, nested.http_p99, nested.requests, nested.failed_rate) == (5.0, 20.0, 12, 0.02)
    assert nested.extra["maxSustainedEvPerSec"] == 2400


def test_parse_k6_without_summary_falls_back_to_run_file():
    run = report.parse_k6("t1", None, RUN_T1)
    assert run.http_p99 == 18.0 and run.requests is None


def test_load_k6_reads_raw_dir(tmp_path: Path):
    (tmp_path / "t1.json").write_text(json.dumps(K6_FLAT))
    (tmp_path / "t1.run.json").write_text(json.dumps(RUN_T1))
    (tmp_path / "t5.json").write_text(json.dumps({"results": []}))
    runs = report.load_k6(tmp_path)
    assert list(runs) == ["t1"]
    since = report.default_since(runs)
    assert since == datetime(2026, 11, 21, 10, 59, 55, tzinfo=UTC)


def test_parse_since_relative_and_iso():
    assert report.parse_since("2h", NOW) == NOW - timedelta(hours=2)
    assert report.parse_since("30m", NOW) == NOW - timedelta(minutes=30)
    assert report.parse_since("2026-11-21T10:00:00Z", NOW) == datetime(2026, 11, 21, 10, tzinfo=UTC)


def test_verdict_against_spec_targets():
    assert report.verdict("ui", report.Latency("ui", 1, 20, 40, 120, 400)) == "PASS"
    assert report.verdict("ui", report.Latency("ui", 1, 60, 80, 120, 400)) == "FAIL"
    assert report.verdict("email", report.Latency("email", 1, 1, 1, 1500, 1500)) == "FAIL"
    assert report.verdict("sms", report.Latency("sms", 1, None, None, None, None)) == "FAIL"
    assert report.verdict("webhook", report.Latency("webhook", 1, 1, 1, 1, 1)) == "n/a"


def test_histogram_bins_clip_outliers():
    edges, counts = report.histogram_bins([1.0] * 99 + [10_000.0], bins=10)
    assert len(edges) == len(counts) == 10
    assert sum(counts) == 100
    assert report.histogram_bins([]) == ([], [])


def test_render_markdown_full():
    md = report.render_markdown(fake_data(), ["latency_histogram.png"])
    assert "# Load test report 2026-11-21 12:00 UTC" in md
    assert "| ui | 2,000 | 20.0 ms | 40.0 ms | 120 ms | 400 ms | PASS (p50 < 50 ms, p99 < 300 ms) |" in md
    assert "FAIL (p99 < 1000 ms)" in md
    assert "max sustained: **2,400 ev/s**" in md
    assert "| 4 | 2,400 | backlog |" in md
    assert "processing latency (all events in window) p50 11.0 ms, p99 240 ms" in md
    assert "peak queue depth: events.in 49" in md
    assert "![latency_histogram](latency_histogram.png)" in md
    assert "LOST = 0" in md


def test_render_markdown_missing_everything_prints_na():
    data = report.ReportData(generated_at=NOW, since=NOW - timedelta(hours=1), until=NOW)
    md = report.render_markdown(data)
    assert md.count("n/a") >= 7
    assert "Prometheus not reachable" in md
    assert "run `make t4`" in md and "run `make t5`" in md


def test_render_charts_writes_pngs(tmp_path: Path):
    written = report.render_charts(fake_data(), tmp_path)
    assert set(written) == {
        "latency_histogram.png",
        "throughput_vs_replicas.png",
        "queue_depth.png",
        "ingest_rate.png",
    }
    for name in written:
        png = (tmp_path / name).read_bytes()
        assert png.startswith(b"\x89PNG") and len(png) > 2000


def test_render_charts_skips_missing_sources(tmp_path: Path):
    data = fake_data(delivery_samples=None, prom=None, t5=None)
    assert report.render_charts(data, tmp_path) == []


def test_end_to_end_report_files(tmp_path: Path):
    data = fake_data()
    charts = report.render_charts(data, tmp_path)
    (tmp_path / "report.md").write_text(report.render_markdown(data, charts))
    text = (tmp_path / "report.md").read_text()
    assert all(c in text for c in charts)
    assert (tmp_path / "latency_histogram.png").stat().st_size > 0
