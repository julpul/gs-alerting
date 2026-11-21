"""Send the demo events to a running backend: the three Appendix A samples, a pricing log line, a poisoned
log line (prompt injection) and a typed `security.secret_leaked` raise.

    uv run python scripts/demo_events.py [--base-url http://localhost:8080] [--storm 500]

`--storm N` additionally sends N identical workflow failures in one batch (dedup demo: 1 alert, count N).
The sources and their keys (`demo-key-*`) come from the seed (`make seed` / `make up`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

SAMPLES = Path(__file__).resolve().parents[1] / "seed" / "fixtures" / "samples"

# (label, sample file, source key, how)
EVENTS = [
    ("settlement workflow failed", "workflow_failed", "demo-key-workflow-broker", "ingest"),
    ("payments-api high error rate", "prometheus_high_error_rate", "demo-key-prometheus", "ingest"),
    ("rejected trade (rates desk)", "trade_rejected", "demo-key-trade-gateway", "ingest"),
    ("pricing engine log line", "pricing_log", "demo-key-pricing-logs", "ingest"),
    ("poisoned log line (injection)", "pricing_log_injection", "demo-key-pricing-logs", "ingest"),
    ("typed raise: secret leaked", "raise_secret_leaked", "demo-key-pr-review-agent", "raise"),
]


def load(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text())


def send_all(client: httpx.Client) -> int:
    failures = 0
    for label, sample, key, how in EVENTS:
        if how == "raise":
            resp = client.post("/v1/alerts/raise", json=load(sample), headers={"X-Api-Key": key})
        else:
            resp = client.post(f"/v1/ingest/{key}", json=load(sample))
        ok = resp.status_code == 202
        failures += not ok
        detail = resp.json().get("eventId") if ok else resp.text
        print(f"{'ok  ' if ok else 'FAIL'} {resp.status_code} {label}: {detail}")
    return failures


def send_storm(client: httpx.Client, count: int) -> int:
    resp = client.post(
        "/v1/ingest/demo-key-workflow-broker/batch", json=[load("workflow_failed") for _ in range(count)]
    )
    ok = resp.status_code == 202
    print(f"{'ok  ' if ok else 'FAIL'} {resp.status_code} storm of {count} identical failures")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--storm", type=int, default=0, metavar="N", help="also send N identical events")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        try:
            failures = send_all(client)
            if args.storm:
                failures += send_storm(client, min(args.storm, 1000))
        except httpx.HTTPError as exc:
            sys.exit(f"cannot reach {args.base_url}: {exc}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
