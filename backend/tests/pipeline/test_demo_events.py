import importlib.util
from pathlib import Path

import httpx
import pytest

from app.pipeline import injection
from app.pipeline.envelope import build_envelope
from app.pipeline.normalize import normalize

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_events.py"
spec = importlib.util.spec_from_file_location("demo_events", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

SOURCE = {"id": "00000000-0000-4000-8000-000000000009", "name": "src", "default_severity": "warning"}


def meta_of(name: str) -> dict:
    envelope = build_envelope(source_id=SOURCE["id"], raw=demo.load(name), priority=3)
    return normalize(envelope, SOURCE).meta


def test_appendix_a_samples_normalize_as_the_rules_expect():
    workflow = meta_of("workflow_failed")
    assert (workflow["schema"], workflow["severity"], workflow["environment"]) == (
        "WORKFLOW_FAILED",
        "error",
        "prod",
    )
    assert workflow["error_code"] == "TIMEOUT" and workflow["timestamp"] == "2026-10-23T09:14:02.331Z"
    prom = meta_of("prometheus_high_error_rate")
    assert (prom["schema"], prom["severity"], prom["service"]) == (
        "HighErrorRate",
        "critical",
        "payments-api",
    )
    trade = meta_of("trade_rejected")
    assert (trade["schema"], trade["source"], trade["timestamp"]) == (
        "trade.rejected.v2",
        "trade-gateway",
        "2026-10-24T09:14:05.000Z",
    )
    pricing = meta_of("pricing_log")
    assert (pricing["source"], pricing["severity"], pricing["flags"]) == ("pricing-engine-3", "error", [])


def test_only_the_injection_sample_is_flagged():
    for name in ("workflow_failed", "prometheus_high_error_rate", "trade_rejected", "pricing_log"):
        assert not injection.scan(demo.load(name)).suspicious
    assert meta_of("pricing_log_injection")["flags"] == ["suspicious_content"]


@pytest.mark.parametrize("storm", [0, 3])
def test_script_posts_every_sample_to_its_demo_key(storm):
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("x-api-key")))
        return httpx.Response(202, json={"eventId": "e1", "accepted": 3})

    with httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler)) as client:
        assert demo.send_all(client) == 0
        if storm:
            assert demo.send_storm(client, storm) == 0
    paths = [p for p, _ in seen]
    assert paths[:5] == [
        "/v1/ingest/demo-key-workflow-broker",
        "/v1/ingest/demo-key-prometheus",
        "/v1/ingest/demo-key-trade-gateway",
        "/v1/ingest/demo-key-pricing-logs",
        "/v1/ingest/demo-key-pricing-logs",
    ]
    assert seen[5] == ("/v1/alerts/raise", "demo-key-pr-review-agent")
    assert (len(seen) == 7) == bool(storm)


def test_script_reports_failures():
    with httpx.Client(
        base_url="http://t", transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no"))
    ) as client:
        assert demo.send_all(client) == 6
        assert demo.send_storm(client, 5) == 1
