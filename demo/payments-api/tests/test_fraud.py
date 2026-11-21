"""Fraud-score behaviour. The slow-service test reproduces the CardGatewayTimeouts incident."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from payments_api.fraud import FraudScoreClient

ACQUIRER_BUDGET_S = 0.5  # the acquirer gives up on an authorisation after this long


def _client(handler) -> FraudScoreClient:
    return FraudScoreClient(url="http://fraud.test/score", transport=httpx.MockTransport(handler))


def test_low_risk_is_approved_and_high_risk_held():
    assert _client(lambda r: httpx.Response(200, json={"score": 0.1})).decision("tok", 1000, "EUR") == "approve"
    assert _client(lambda r: httpx.Response(200, json={"score": 0.9})).decision("tok", 1000, "EUR") == "review"


@pytest.fixture
def slow_fraud_service() -> Iterator[str]:
    """A real HTTP fraud-score service that answers after 2 s (degraded, as in the incident)."""

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            time.sleep(2)
            body = json.dumps({"score": 0.1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/score"
    server.shutdown()


def test_slow_fraud_score_does_not_block_authorisation(slow_fraud_service):
    started = time.perf_counter()
    decision = FraudScoreClient(url=slow_fraud_service).decision("tok", 1000, "EUR")
    elapsed = time.perf_counter() - started
    assert decision == "review"  # no score in time: hold for manual review instead of waiting
    assert elapsed < ACQUIRER_BUDGET_S
