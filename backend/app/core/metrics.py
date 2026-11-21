"""All Prometheus metrics in one place (names fixed by plan 3.6)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

INGESTED = Counter("alerting_ingested_total", "Events accepted at ingest", ["source"])
EVENTS_OUTCOME = Counter(
    "alerting_events_outcome_total",
    "Final outcome of each processed event (alerted_new|deduplicated|grouped|unrouted|invalid|auto_resolved)",
    ["outcome"],
)
PROCESSING_LATENCY = Histogram(
    "alerting_processing_latency_seconds",
    "Alert commit time minus event received_at",
    buckets=LATENCY_BUCKETS,
)
DELIVERY_LATENCY = Histogram(
    "alerting_delivery_latency_seconds",
    "Delivery time minus event received_at",
    ["channel"],
    buckets=LATENCY_BUCKETS,
)
DELIVERIES = Counter("alerting_deliveries_total", "Deliveries by final status", ["channel", "status"])
RULE_EVAL = Histogram(
    "alerting_rule_eval_seconds",
    "Time to evaluate candidate rules for one event",
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1),
)
SSE_CLIENTS = Gauge("alerting_sse_clients", "Connected SSE clients on this process")
OUTBOX_UNPUBLISHED = Gauge("alerting_outbox_unpublished", "Outbox rows waiting for publish")
