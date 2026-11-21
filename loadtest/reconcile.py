#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["asyncpg", "httpx"]
# ///
"""Reconciliation for the no-loss test (T4) and the dedup test (T6).

    uv run loadtest/reconcile.py chaos --run-file loadtest/results/raw/t4.run.json [--killed]
    uv run loadtest/reconcile.py dedup --run-file loadtest/results/raw/t6.run.json
    uv run loadtest/reconcile.py drain

`chaos` waits for the queues to drain, then checks three independent things and prints ``LOST = N``
(exit code 1 if N > 0):

  A. deliveries  created = delivered + dead-lettered(with a delivered fallback) + suppressed_digest
  B. alerts      every accepted, matching, unique-key event of this run produced an alert
  C. counters    accepted events = sum of alerting_events_outcome_total (Prometheus, needs PROM_URL)

Env: DATABASE_URL  PROM_URL  RABBIT_URL  RABBIT_AUTH  API_URL  DRAIN_TIMEOUT (300)  SETTLE_TIMEOUT (90)
     OK_STATUSES (sent,delivered,acked)  COUNTER_SLACK (500, only applied when --killed)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import common
import httpx

OK_STATUSES = [s for s in os.environ.get("OK_STATUSES", "sent,delivered,acked").split(",") if s]
OUTCOMES = ("alerted_new", "deduplicated", "grouped", "unrouted", "invalid")

# Deliveries are scoped to the alerts created by this load run (run id embedded in raw_event).
SQL_DELIVERY_SUMMARY = """
SELECT count(*)::int AS total,
       count(*) FILTER (WHERE d.status = ANY($3::text[]))::int AS ok,
       count(*) FILTER (WHERE d.status = 'suppressed_digest')::int AS suppressed,
       count(*) FILTER (WHERE d.status = 'dead_lettered')::int AS dead_lettered,
       count(*) FILTER (WHERE d.status = 'dead_lettered' AND EXISTS (
           SELECT 1 FROM deliveries f
           WHERE f.alert_id = d.alert_id AND f.id <> d.id AND f.channel <> d.channel
             AND f.status = ANY($3::text[]) AND f.created_at >= d.created_at))::int AS dead_lettered_fallback
FROM deliveries d JOIN alerts a ON a.id = d.alert_id
WHERE a.created_at >= $1 AND a.raw_event::text LIKE $2
"""

SQL_DELIVERY_STATUSES = """
SELECT d.status, count(*)::int AS n
FROM deliveries d JOIN alerts a ON a.id = d.alert_id
WHERE a.created_at >= $1 AND a.raw_event::text LIKE $2
GROUP BY d.status ORDER BY n DESC
"""

# Pricing alerts are grouped (not one per event) so they are excluded from the per-event expectation.
SQL_ALERTS_FOUND = """
SELECT count(*)::int FROM alerts a
WHERE a.created_at >= $1 AND a.raw_event::text LIKE $2 AND a.raw_event::text NOT LIKE '%pricing-engine-%'
"""

SQL_DEDUP_ALERTS = """
SELECT a.id, a.short_id, a.count, a.status
FROM alerts a WHERE a.created_at >= $1 AND a.raw_event::text LIKE $2
"""

SQL_DEDUP_DELIVERIES = """
SELECT channel, kind, recipient_user_id::text AS recipient, count(*)::int AS n
FROM deliveries WHERE alert_id = $1 GROUP BY channel, kind, recipient_user_id ORDER BY channel
"""


@dataclass
class DeliveryStats:
    total: int = 0
    ok: int = 0
    suppressed: int = 0
    dead_lettered: int = 0
    dead_lettered_fallback: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def lost(self) -> int:
        return max(0, self.total - self.ok - self.suppressed - self.dead_lettered_fallback)


@dataclass
class ChaosInputs:
    accepted: int
    expected_alerts: int
    found_alerts: int
    deliveries: DeliveryStats
    outcomes: dict[str, float] | None
    prom_ingested: float | None
    drained: bool
    pending: dict[str, int]
    killed: bool
    counter_slack: int


@dataclass
class ChaosResult:
    lost_alerts: int
    lost_deliveries: int
    counter_gap: int | None
    counter_gap_counted: int
    queue_residue: int
    lost: int
    notes: list[str]


def compute_chaos(inp: ChaosInputs) -> ChaosResult:
    """Pure reconciliation arithmetic (see module docstring)."""
    notes: list[str] = []
    lost_alerts = max(0, inp.expected_alerts - inp.found_alerts)
    if inp.expected_alerts > 0 and inp.found_alerts == 0:
        notes.append("no alert of this run found: raw_event may not contain the run id")

    counter_gap: int | None = None
    counted = 0
    if inp.outcomes is not None:
        outcome_sum = sum(inp.outcomes.values())
        counter_gap = round(inp.accepted - outcome_sum)
        slack = inp.counter_slack if inp.killed else 0
        counted = max(0, counter_gap - slack)
        if counter_gap > 0 and counted == 0:
            notes.append(
                f"counter gap {counter_gap} within kill slack {slack}: the killed worker's last scrape interval "
                "is not in Prometheus; alert and delivery checks are the ground truth"
            )
        if counter_gap < 0:
            notes.append(f"outcomes exceed accepted by {-counter_gap}: redelivered messages counted twice")
    else:
        notes.append("counter check n/a: Prometheus not reachable")

    residue = sum(inp.pending.values()) if not inp.drained else 0
    if residue:
        notes.append(f"queues did not drain: {inp.pending}")

    lost = lost_alerts + inp.deliveries.lost + counted + residue
    return ChaosResult(lost_alerts, inp.deliveries.lost, counter_gap, counted, residue, lost, notes)


def format_chaos(inp: ChaosInputs, res: ChaosResult) -> str:
    d = inp.deliveries
    gap = "n/a" if res.counter_gap is None else str(res.counter_gap)
    outcomes = (
        "n/a"
        if inp.outcomes is None
        else ", ".join(f"{k}={int(inp.outcomes.get(k, 0))}" for k in OUTCOMES)
        + f"  (sum {int(sum(inp.outcomes.values()))})"
    )
    lines = [
        "== Reconciliation (T4) ==",
        f"events accepted by API (k6 202s) : {inp.accepted}",
        f"prometheus ingested_total        : {'n/a' if inp.prom_ingested is None else int(inp.prom_ingested)}",
        f"outcomes                         : {outcomes}",
        f"counter gap (accepted - outcomes): {gap}  counted as lost: {res.counter_gap_counted}",
        f"alerts expected / found          : {inp.expected_alerts} / {inp.found_alerts}  lost: {res.lost_alerts}",
        f"deliveries created               : {d.total}",
        f"  delivered (ok statuses)        : {d.ok}",
        f"  dead-lettered, fallback ok     : {d.dead_lettered_fallback} (dead-lettered total {d.dead_lettered})",
        f"  suppressed_digest              : {d.suppressed}",
        f"  unaccounted                    : {d.lost}",
        f"  by status                      : {d.statuses}",
        f"queue residue after drain        : {res.queue_residue}",
    ]
    lines += [f"note: {n}" for n in res.notes]
    lines.append(f"LOST = {res.lost}")
    return "\n".join(lines)


def check_dedup(
    alerts: list[dict[str, Any]], deliveries: list[dict[str, Any]], total: int
) -> tuple[bool, list[str]]:
    """T6 verdict: 1 alert with count == total and at most one hard delivery per channel+recipient."""
    msgs: list[str] = []
    ok = True
    if len(alerts) != 1:
        ok = False
        msgs.append(f"expected exactly 1 alert for this run, found {len(alerts)}")
    else:
        msgs.append(
            f"alert A-{alerts[0]['short_id']} count={alerts[0]['count']} status={alerts[0]['status']}"
        )
        if alerts[0]["count"] != total:
            ok = False
            msgs.append(f"expected count {total}, got {alerts[0]['count']}")
    hard = [r for r in deliveries if r["kind"] == "hard"]
    if not hard:
        msgs.append("no hard deliveries found (hard channels not configured for this rule?)")
    for r in hard:
        msgs.append(f"hard delivery channel={r['channel']} recipient={r['recipient']} n={r['n']}")
        if r["n"] != 1:
            ok = False
            msgs.append(f"  -> {r['n']} notifications for one recipient/channel; expected exactly 1")
    return ok, msgs


# ------------------------------------------------------------------ I/O


def run_scope(run_id: str) -> str:
    return f"%{run_id}%"


async def fetch_delivery_stats(conn: asyncpg.Connection, since: datetime, run_id: str) -> DeliveryStats:
    row = await conn.fetchrow(SQL_DELIVERY_SUMMARY, since, run_scope(run_id), OK_STATUSES)
    statuses = await conn.fetch(SQL_DELIVERY_STATUSES, since, run_scope(run_id))
    return DeliveryStats(**dict(row), statuses={r["status"]: r["n"] for r in statuses})


async def settle(conn: asyncpg.Connection, since: datetime, run_id: str, timeout_s: float) -> DeliveryStats:
    """Fallback deliveries may still be created right after the queues drain; wait until nothing is unaccounted."""
    deadline = time.monotonic() + timeout_s
    stats = await fetch_delivery_stats(conn, since, run_id)
    while stats.lost > 0 and time.monotonic() < deadline:
        await asyncio.sleep(3)
        stats = await fetch_delivery_stats(conn, since, run_id)
    return stats


def fetch_outcomes(start: datetime, end: datetime) -> tuple[dict[str, float] | None, float | None]:
    if not common.prom_reachable():
        return None, None
    try:
        out = common.prom_query_range("alerting_events_outcome_total", start, end)
        ing = common.prom_query_range("alerting_ingested_total", start, end)
    except (httpx.HTTPError, KeyError, ValueError):
        return None, None
    return common.group_increase(out, "outcome"), common.series_increase(ing)


def fetch_summary() -> dict[str, Any] | None:
    try:
        login = httpx.post(
            f"{common.API_URL}/v1/auth/login", json={"email": "master@demo", "password": "demo"}, timeout=10
        )
        token = login.json()["accessToken"]
        resp = httpx.get(
            f"{common.API_URL}/v1/metrics/summary", headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, KeyError, ValueError):
        return None


async def connect() -> asyncpg.Connection:
    try:
        return await asyncpg.connect(common.DATABASE_URL, timeout=10)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        raise SystemExit(
            f"cannot connect to {common.DATABASE_URL.split('@')[-1]}: {exc.__class__.__name__}"
        ) from exc


async def cmd_chaos(args: argparse.Namespace) -> int:
    run = common.load_json(args.run_file)
    if not run or not run.get("runId"):
        print(f"cannot read run id from {args.run_file}", file=sys.stderr)
        return 2
    since = common.parse_ts(run["startedAt"])
    drained, pending, waited = common.wait_drain(float(os.environ.get("DRAIN_TIMEOUT", "300")))
    print(f"queues drained={drained} after {waited:.0f}s", flush=True)
    await asyncio.sleep(6)  # let Prometheus take two more scrapes
    conn = await connect()
    try:
        stats = await settle(conn, since, run["runId"], float(os.environ.get("SETTLE_TIMEOUT", "90")))
        found = await conn.fetchval(SQL_ALERTS_FOUND, since, run_scope(run["runId"]))
    finally:
        await conn.close()
    end = datetime.now(UTC)
    outcomes, prom_ingested = fetch_outcomes(since - timedelta(seconds=10), end)
    counts = run.get("counts", {})
    inp = ChaosInputs(
        accepted=int(counts.get("accepted", 0)),
        expected_alerts=int(counts.get("expectedAlerts", 0)) + int(counts.get("expectedAlertsTyped", 0)),
        found_alerts=int(found),
        deliveries=stats,
        outcomes=outcomes,
        prom_ingested=prom_ingested,
        drained=drained,
        pending=pending,
        killed=args.killed,
        counter_slack=int(os.environ.get("COUNTER_SLACK", "500")),
    )
    res = compute_chaos(inp)
    print(format_chaos(inp, res))
    summary = fetch_summary()
    if summary is not None:
        print(f"/v1/metrics/summary: lost={summary.get('lost')} ingested={summary.get('eventsIngested')}")
    return 1 if res.lost > 0 else 0


async def cmd_dedup(args: argparse.Namespace) -> int:
    run = common.load_json(args.run_file)
    if not run or not run.get("runId"):
        print(f"cannot read run id from {args.run_file}", file=sys.stderr)
        return 2
    since = common.parse_ts(run["startedAt"])
    total = int(args.total or run.get("total") or run["counts"]["accepted"])
    drained, _, waited = common.wait_drain(float(os.environ.get("DRAIN_TIMEOUT", "120")))
    print(f"queues drained={drained} after {waited:.0f}s")
    conn = await connect()
    try:
        alerts = [dict(r) for r in await conn.fetch(SQL_DEDUP_ALERTS, since, run_scope(run["runId"]))]
        deliveries = (
            [dict(r) for r in await conn.fetch(SQL_DEDUP_DELIVERIES, alerts[0]["id"])] if alerts else []
        )
    finally:
        await conn.close()
    ok, msgs = check_dedup(alerts, deliveries, total)
    print("== Dedup (T6) ==")
    print("\n".join(msgs))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_drain(_: argparse.Namespace) -> int:
    drained, pending, waited = common.wait_drain(float(os.environ.get("DRAIN_TIMEOUT", "300")))
    print(f"drained={drained} waited={waited:.0f}s pending={pending}")
    return 0 if drained else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chaos")
    c.add_argument("--run-file", required=True)
    c.add_argument(
        "--killed", action="store_true", help="a worker was killed during the run (applies COUNTER_SLACK)"
    )
    d = sub.add_parser("dedup")
    d.add_argument("--run-file", required=True)
    d.add_argument("--total", type=int)
    sub.add_parser("drain")
    args = p.parse_args(argv)
    if args.cmd == "chaos":
        return asyncio.run(cmd_chaos(args))
    if args.cmd == "dedup":
        return asyncio.run(cmd_dedup(args))
    return cmd_drain(args)


if __name__ == "__main__":
    sys.exit(main())
