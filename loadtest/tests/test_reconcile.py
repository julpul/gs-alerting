"""Reconciliation arithmetic and queue draining, with fakes (no infrastructure)."""

from __future__ import annotations

import common
import reconcile


def inputs(**kw):
    base = {
        "accepted": 1000,
        "expected_alerts": 200,
        "found_alerts": 200,
        "deliveries": reconcile.DeliveryStats(
            total=300, ok=250, suppressed=20, dead_lettered=40, dead_lettered_fallback=30
        ),
        "outcomes": {"alerted_new": 200, "unrouted": 790, "deduplicated": 10},
        "prom_ingested": 1000.0,
        "drained": True,
        "pending": {},
        "killed": False,
        "counter_slack": 500,
    }
    base.update(kw)
    return reconcile.ChaosInputs(**base)


def test_no_loss():
    res = reconcile.compute_chaos(inputs())
    assert res.lost == 0 and res.lost_deliveries == 0 and res.counter_gap == 0
    assert "LOST = 0" in reconcile.format_chaos(inputs(), res)


def test_dead_letter_without_fallback_is_lost():
    stats = reconcile.DeliveryStats(
        total=300, ok=250, suppressed=20, dead_lettered=40, dead_lettered_fallback=20
    )
    res = reconcile.compute_chaos(inputs(deliveries=stats))
    assert res.lost_deliveries == 10 and res.lost == 10


def test_missing_alerts_are_lost():
    res = reconcile.compute_chaos(inputs(found_alerts=195))
    assert res.lost_alerts == 5 and res.lost == 5


def test_counter_gap_counts_unless_worker_killed():
    outcomes = {"alerted_new": 200, "unrouted": 700}
    assert reconcile.compute_chaos(inputs(outcomes=outcomes)).lost == 100
    killed = reconcile.compute_chaos(inputs(outcomes=outcomes, killed=True))
    assert killed.lost == 0 and any("kill slack" in n for n in killed.notes)
    big = reconcile.compute_chaos(inputs(outcomes={"alerted_new": 100}, killed=True))
    assert big.lost == 400


def test_outcomes_above_accepted_is_a_note_not_loss():
    res = reconcile.compute_chaos(inputs(outcomes={"alerted_new": 1010}))
    assert res.lost == 0 and any("twice" in n for n in res.notes)


def test_prometheus_missing_skips_counter_check():
    res = reconcile.compute_chaos(inputs(outcomes=None, prom_ingested=None))
    assert res.counter_gap is None and res.lost == 0
    assert "counter gap (accepted - outcomes): n/a" in reconcile.format_chaos(inputs(outcomes=None), res)


def test_undrained_queues_count_as_lost():
    res = reconcile.compute_chaos(inputs(drained=False, pending={"events.in": 12, "deliveries.sms": 3}))
    assert res.queue_residue == 15 and res.lost == 15


def test_no_alert_found_hints_at_run_id():
    res = reconcile.compute_chaos(inputs(found_alerts=0))
    assert res.lost_alerts == 200 and any("run id" in n for n in res.notes)


def test_check_dedup_pass_and_fail():
    alerts = [{"short_id": 7, "count": 500, "status": "open"}]
    hard = [
        {"channel": "sms", "kind": "hard", "recipient": "u1", "n": 1},
        {"channel": "phone", "kind": "hard", "recipient": "u1", "n": 1},
    ]
    ok, _ = reconcile.check_dedup(alerts, hard, 500)
    assert ok
    ok, msgs = reconcile.check_dedup([{"short_id": 7, "count": 499, "status": "open"}], hard, 500)
    assert not ok and any("expected count 500" in m for m in msgs)
    ok, _ = reconcile.check_dedup(alerts, [{**hard[0], "n": 2}], 500)
    assert not ok
    ok, msgs = reconcile.check_dedup([], [], 500)
    assert not ok and "found 0" in msgs[0]


def test_counter_increase_handles_restart():
    # killed worker restarts: 100 -> 0 -> 30 counts 30 new events after the reset
    assert common.counter_increase([(1, 0), (2, 40), (3, 100), (4, 5), (5, 30)]) == 130
    assert common.counter_increase([]) == 0 and common.counter_increase([(1, 5)]) == 0


def test_group_increase_sums_instances_by_label():
    series = [
        {"metric": {"outcome": "alerted_new", "instance": "w1"}, "values": [[1, "0"], [2, "10"]]},
        {"metric": {"outcome": "alerted_new", "instance": "w2"}, "values": [[1, "0"], [2, "5"]]},
        {"metric": {"outcome": "unrouted", "instance": "w1"}, "values": [[1, "1"], [2, "4"]]},
    ]
    assert common.group_increase(series, "outcome") == {"alerted_new": 15, "unrouted": 3}


def test_pending_ignores_dlq_and_unrelated_queues():
    depths = {
        "events.in": 4,
        "deliveries.dlq": 99,
        "deliveries.sms": 0,
        "deliveries.retry.30s": 2,
        "ai.tasks": 7,
    }
    assert common.pending_in_drain_queues(depths) == {"events.in": 4, "deliveries.retry.30s": 2}


def test_wait_drain_polls_until_empty_twice():
    seq = iter([{"events.in": 5}, {"events.in": 1}, {}, {}, {}])
    now = [0.0]
    drained, pending, _ = common.wait_drain(
        60,
        poll_s=1,
        fetch=lambda: next(seq),
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
    )
    assert drained and pending == {}


def test_wait_drain_times_out():
    now = [0.0]
    drained, pending, waited = common.wait_drain(
        5,
        poll_s=2,
        fetch=lambda: {"events.in": 9},
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
    )
    assert not drained and pending == {"events.in": 9} and waited >= 5
