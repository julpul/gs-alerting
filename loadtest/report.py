#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["asyncpg", "httpx", "matplotlib"]
# ///
"""Load test report: k6 summaries + SQL latency percentiles + Prometheus -> markdown and PNG charts.

    uv run loadtest/report.py --since 2026-11-21T10:00:00Z [--until ...] [--out loadtest/results/<name>]

Reads loadtest/results/raw/{t1,t2,t4,t6,t5}.json (+ .run.json written by the k6 scripts).
Every source is optional: what is unavailable is rendered as "n/a".

Env: DATABASE_URL  PROM_URL  RAW_DIR
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import common
import httpx
from charts import histogram_bins, render_charts  # noqa: F401  (re-exported for tests)

CHANNELS = ("ui", "email", "sms", "phone")
# Spec §18 targets, milliseconds.
TARGETS_MS = {
    "ui": {"p50": 50, "p99": 300},
    "email": {"p99": 1000},
    "sms": {"p99": 1000},
    "phone": {"p99": 1000},
}
SAMPLE_LIMIT = 200_000

SQL_DELIVERY_PERCENTILES = """
SELECT channel,
       count(*)::int AS n,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY ms) AS p99,
       max(ms) AS max
FROM (SELECT channel, extract(epoch FROM delivered_at - event_received_at) * 1000 AS ms
      FROM deliveries
      WHERE delivered_at IS NOT NULL AND event_received_at >= $1 AND event_received_at < $2) t
GROUP BY channel ORDER BY channel
"""

SQL_DELIVERY_SAMPLES = """
SELECT channel, extract(epoch FROM delivered_at - event_received_at) * 1000 AS ms
FROM deliveries
WHERE delivered_at IS NOT NULL AND event_received_at >= $1 AND event_received_at < $2
LIMIT $3
"""

SQL_ALERT_LATENCY = """
SELECT count(*)::int AS n,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY ms) AS p99,
       max(ms) AS max
FROM (SELECT extract(epoch FROM created_at - first_event_received_at) * 1000 AS ms
      FROM alerts WHERE first_event_received_at >= $1 AND first_event_received_at < $2) t
"""


@dataclass
class K6Run:
    name: str
    http_p50: float | None = None
    http_p95: float | None = None
    http_p99: float | None = None
    requests: int | None = None
    failed_rate: float | None = None
    run_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    accepted: int | None = None
    rejected: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Latency:
    label: str
    n: int
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None


@dataclass
class PromData:
    ingest_rate: list[tuple[float, float]] = field(default_factory=list)
    processing_p50_ms: float | None = None
    processing_p99_ms: float | None = None
    outcomes: dict[str, float] = field(default_factory=dict)
    ingested: float | None = None
    queue_depth: dict[str, list[tuple[float, float]]] = field(default_factory=dict)


@dataclass
class ReportData:
    generated_at: datetime
    since: datetime
    until: datetime
    api_url: str = "n/a"
    k6: dict[str, K6Run] = field(default_factory=dict)
    delivery_latency: list[Latency] | None = None
    delivery_samples: dict[str, list[float]] | None = None
    alert_latency: Latency | None = None
    prom: PromData | None = None
    t5: list[dict[str, Any]] | None = None
    t4_reconcile: str | None = None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ parsing (pure)


def _values(metric: dict[str, Any] | None) -> dict[str, Any]:
    """k6 summary metrics are either {"values": {...}} (old) or flat (new)."""
    if not metric:
        return {}
    return metric.get("values", metric)


def parse_k6(name: str, summary: dict[str, Any] | None, run: dict[str, Any] | None) -> K6Run:
    metrics = (summary or {}).get("metrics", {})
    dur = _values(metrics.get("http_req_duration"))
    reqs = _values(metrics.get("http_reqs"))
    failed = _values(metrics.get("http_req_failed"))
    out = K6Run(
        name=name,
        http_p50=dur.get("med"),
        http_p95=dur.get("p(95)"),
        http_p99=dur.get("p(99)"),
        requests=int(reqs["count"]) if "count" in reqs else None,
        failed_rate=failed.get("rate", failed.get("value")),
    )
    if run:
        counts = run.get("counts", {})
        out.run_id = run.get("runId")
        out.started_at = run.get("startedAt")
        out.ended_at = run.get("endedAt")
        out.duration_s = run.get("durationS")
        out.accepted = counts.get("accepted")
        out.rejected = counts.get("rejected")
        if out.http_p50 is None and run.get("httpMs"):
            out.http_p50, out.http_p95, out.http_p99 = (run["httpMs"].get(k) for k in ("p50", "p95", "p99"))
        out.extra = {k: v for k, v in run.items() if k not in {"counts", "httpMs", "test", "runId"}}
    return out


def load_k6(raw_dir: Path) -> dict[str, K6Run]:
    names = sorted(
        {p.name.removesuffix(".run.json").removesuffix(".json") for p in raw_dir.glob("*.json")}
        - {"t5", "t4.killed"}
    )
    runs: dict[str, K6Run] = {}
    for name in names:
        summary = common.load_json(raw_dir / f"{name}.json")
        run = common.load_json(raw_dir / f"{name}.run.json")
        if summary or run:
            runs[name] = parse_k6(name, summary, run)
    return runs


def default_since(runs: dict[str, K6Run]) -> datetime | None:
    starts = [common.parse_ts(r.started_at) for r in runs.values() if r.started_at]
    return min(starts) - timedelta(seconds=5) if starts else None


def parse_since(value: str, now: datetime) -> datetime:
    """Accept ISO timestamps or relative values like 30m, 2h, 1d."""
    m = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if m:
        seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return now - timedelta(seconds=seconds)
    return common.parse_ts(value)


def verdict(channel: str, lat: Latency) -> str:
    target = TARGETS_MS.get(channel)
    if not target:
        return "n/a"
    ok = all(getattr(lat, k) is not None and getattr(lat, k) < limit for k, limit in target.items())
    return "PASS" if ok else "FAIL"


# ------------------------------------------------------------------ formatting (pure)


def ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f} ms" if value >= 100 else f"{value:.1f} ms"


def num(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:,.0f}{suffix}"


def _latency_table(rows: list[Latency], with_verdict: bool) -> list[str]:
    head = "| Channel | n | p50 | p95 | p99 | max |" + (" Target |" if with_verdict else "")
    sep = "| --- | ---: | ---: | ---: | ---: | ---: |" + (" --- |" if with_verdict else "")
    lines = [head, sep]
    for r in rows:
        line = f"| {r.label} | {r.n:,} | {ms(r.p50)} | {ms(r.p95)} | {ms(r.p99)} | {ms(r.max)} |"
        if with_verdict:
            target = TARGETS_MS.get(r.label, {})
            desc = ", ".join(f"{k} < {v} ms" for k, v in target.items()) or "-"
            line += f" {verdict(r.label, r)} ({desc}) |"
        lines.append(line)
    return lines


def _k6_block(run: K6Run | None, title: str) -> list[str]:
    if run is None:
        return [f"## {title}", "", "n/a (no k6 summary found; run the test first)", ""]
    lines = [
        f"## {title}",
        "",
        f"- run id: `{run.run_id or 'n/a'}`, {run.duration_s or 'n/a'} s",
        f"- window: {run.started_at or 'n/a'} to {run.ended_at or 'n/a'}",
        f"- events accepted {num(run.accepted)}, rejected {num(run.rejected)}, http requests {num(run.requests)}",
        f"- API http latency p50 {ms(run.http_p50)}, p95 {ms(run.http_p95)}, p99 {ms(run.http_p99)}",
    ]
    if run.failed_rate is not None:
        lines.append(f"- http failure rate {run.failed_rate * 100:.2f} %")
    return lines


def render_markdown(data: ReportData, charts: list[str] | None = None) -> str:
    charts = charts or []
    out = [
        f"# Load test report {data.generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        f"- window: {data.since:%Y-%m-%d %H:%M:%S} to {data.until:%Y-%m-%d %H:%M:%S} UTC",
        f"- API: {data.api_url}",
        "",
    ]
    out += _k6_block(data.k6.get("t1"), "T1 latency (100 ev/s, mixed, 20 % matching)")
    out += ["", "### Delivery latency per channel (`delivered_at - event_received_at`, SQL)", ""]
    if data.delivery_latency:
        out += _latency_table(data.delivery_latency, with_verdict=True)
    else:
        out.append("n/a (database not reachable or no delivered rows in the window)")
    out += ["", "### Alert creation latency (`created_at - first_event_received_at`, SQL)", ""]
    if data.alert_latency and data.alert_latency.n:
        out += _latency_table([data.alert_latency], with_verdict=False)
    else:
        out.append("n/a")

    out += [""] + _k6_block(data.k6.get("t2"), "T2 throughput (ramping, batch endpoint)")
    t2 = data.k6.get("t2")
    if t2:
        out.append(
            f"- max sustained: **{num(t2.extra.get('maxSustainedEvPerSec'), ' ev/s')}**, "
            f"accepted average {num(t2.extra.get('acceptedEvPerSec'), ' ev/s')}, "
            f"stop reason: {t2.extra.get('stopReason', 'n/a')}"
        )

    out += ["", "## T4 chaos (no loss)", ""]
    out.append(f"```\n{data.t4_reconcile.strip()}\n```" if data.t4_reconcile else "n/a (run `make t4`)")

    out += ["", "## T5 scaling", ""]
    if data.t5:
        out += ["| Workers | Max sustained ev/s | Stop reason |", "| ---: | ---: | --- |"]
        out += [
            f"| {r['workers']} | {num(r.get('maxSustainedEvPerSec'))} | {r.get('stopReason') or 'n/a'} |"
            for r in data.t5
        ]
    else:
        out.append("n/a (run `make t5`)")

    out += [""] + _k6_block(data.k6.get("t6"), "T6 dedup (500 identical events)")

    out += ["", "## Pipeline metrics (Prometheus)", ""]
    prom = data.prom
    if prom is None:
        out.append("n/a (Prometheus not reachable; set `PROM_URL`)")
    else:
        out.append(
            f"- processing latency (all events in window) p50 {ms(prom.processing_p50_ms)}, p99 {ms(prom.processing_p99_ms)}"
        )
        out.append(f"- ingested events {num(prom.ingested)}")
        if prom.outcomes:
            out.append("- outcomes: " + ", ".join(f"{k} {num(v)}" for k, v in sorted(prom.outcomes.items())))
        if prom.queue_depth:
            peak = {q: max(v for _, v in pts) for q, pts in prom.queue_depth.items() if pts}
            out.append("- peak queue depth: " + ", ".join(f"{q} {num(v)}" for q, v in sorted(peak.items())))
    if charts:
        out += ["", "## Charts", ""] + [f"![{Path(c).stem}]({c})" for c in charts]
    if data.notes:
        out += ["", "## Notes", ""] + [f"- {n}" for n in data.notes]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ data access


def _lat(label: str, row: Any) -> Latency:
    return Latency(label, row["n"], row["p50"], row["p95"], row["p99"], row["max"])


async def fetch_sql(
    since: datetime, until: datetime
) -> tuple[list[Latency], dict[str, list[float]], Latency | None]:
    conn = await asyncpg.connect(common.DATABASE_URL, timeout=5)
    try:
        rows = await conn.fetch(SQL_DELIVERY_PERCENTILES, since, until)
        samples: dict[str, list[float]] = {}
        for r in await conn.fetch(SQL_DELIVERY_SAMPLES, since, until, SAMPLE_LIMIT):
            samples.setdefault(r["channel"], []).append(float(r["ms"]))
        alert = await conn.fetchrow(SQL_ALERT_LATENCY, since, until)
    finally:
        await conn.close()
    return [_lat(r["channel"], r) for r in rows], samples, (_lat("alerts", alert) if alert else None)


def _instant(query: str, at: datetime) -> float | None:
    resp = httpx.get(
        f"{common.PROM_URL}/api/v1/query", params={"query": query, "time": at.timestamp()}, timeout=15
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else value


def fetch_prom(since: datetime, until: datetime) -> PromData | None:
    if not common.prom_reachable():
        return None
    window = max(1, int((until - since).total_seconds()))
    step = max(2.0, window / 800)
    bucket = "alerting_processing_latency_seconds_bucket"
    prom = PromData()
    try:
        for q, attr in ((0.5, "processing_p50_ms"), (0.99, "processing_p99_ms")):
            v = _instant(f"histogram_quantile({q}, sum by (le) (increase({bucket}[{window}s])))", until)
            setattr(prom, attr, None if v is None else v * 1000)
        rate = common.prom_query_range("sum(rate(alerting_ingested_total[15s]))", since, until, step)
        prom.ingest_rate = [(float(t), float(v)) for t, v in rate[0]["values"]] if rate else []
        prom.outcomes = common.group_increase(
            common.prom_query_range("alerting_events_outcome_total", since, until, step), "outcome"
        )
        prom.ingested = common.series_increase(
            common.prom_query_range("alerting_ingested_total", since, until, step)
        )
        queues = common.prom_query_range(
            'max by (queue) (rabbitmq_detailed_queue_messages_ready{queue=~"events.in|deliveries.*"})',
            since,
            until,
            step,
        )
        prom.queue_depth = {
            s["metric"].get("queue", "?"): [(float(t), float(v)) for t, v in s["values"]] for s in queues
        }
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        return prom
    return prom


def collect(since: datetime | None, until: datetime) -> ReportData:
    raw_dir = common.RAW_DIR
    k6 = load_k6(raw_dir)
    since = since or default_since(k6) or (until - timedelta(hours=1))
    data = ReportData(generated_at=datetime.now(UTC), since=since, until=until, api_url=common.API_URL, k6=k6)
    t5 = common.load_json(raw_dir / "t5.json")
    data.t5 = t5.get("results") if t5 else None
    recon = raw_dir / "t4.reconcile.txt"
    data.t4_reconcile = recon.read_text() if recon.exists() else None
    try:
        data.delivery_latency, data.delivery_samples, data.alert_latency = asyncio.run(
            fetch_sql(since, until)
        )
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        data.notes.append(f"SQL percentiles unavailable: {exc.__class__.__name__}: {exc}")
    data.prom = fetch_prom(since, until)
    if data.prom is None:
        data.notes.append(f"Prometheus not reachable at {common.PROM_URL}")
    return data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--since", help="ISO timestamp or relative (30m, 2h). Default: first k6 run in results/raw"
    )
    p.add_argument("--until", help="ISO timestamp, default now")
    p.add_argument("--out", help="output directory, default loadtest/results/<YYYY-MM-DD_HHMM>")
    args = p.parse_args(argv)
    now = datetime.now(UTC)
    until = parse_since(args.until, now) if args.until else now
    since = parse_since(args.since, now) if args.since else None
    data = collect(since, until)
    outdir = (
        Path(args.out) if args.out else common.LOADTEST_DIR / "results" / f"{now.astimezone():%Y-%m-%d_%H%M}"
    )
    outdir.mkdir(parents=True, exist_ok=True)
    charts = render_charts(data, outdir)
    (outdir / "report.md").write_text(render_markdown(data, charts))
    print(f"report written to {outdir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
