from uuid import uuid4

from app.pipeline.fingerprint import compute_fingerprint, knowledge_key

RULE = uuid4()


def test_same_inputs_same_fingerprint():
    assert compute_fingerprint(RULE, ["a", 1]) == compute_fingerprint(str(RULE), ["a", 1])
    assert len(compute_fingerprint(RULE, [])) == 40


def test_rule_and_values_change_the_fingerprint():
    base = compute_fingerprint(RULE, ["T-1"])
    assert compute_fingerprint(uuid4(), ["T-1"]) != base
    assert compute_fingerprint(RULE, ["T-2"]) != base
    assert compute_fingerprint(RULE, ["T-1", "x"]) != base


def test_values_do_not_collide_across_boundaries():
    assert compute_fingerprint(RULE, ["ab", "c"]) != compute_fingerprint(RULE, ["a", "bc"])
    assert compute_fingerprint(RULE, [None]) == compute_fingerprint(RULE, [""])


def test_structured_values_are_canonical():
    assert compute_fingerprint(RULE, [{"a": 1, "b": 2}]) == compute_fingerprint(RULE, [{"b": 2, "a": 1}])


def test_grouping_window_buckets_by_time_and_key():
    base = dict(window_seconds=300)
    a = compute_fingerprint(RULE, ["t"], group_value="EURUSD", now=1000.0, **base)
    assert a == compute_fingerprint(RULE, ["t"], group_value="EURUSD", now=1199.0, **base)
    assert a != compute_fingerprint(RULE, ["t"], group_value="EURUSD", now=1201.0, **base)
    assert a != compute_fingerprint(RULE, ["t"], group_value="GBPUSD", now=1000.0, **base)
    assert a != compute_fingerprint(RULE, ["t"])


def test_knowledge_key():
    domain = uuid4()
    key = knowledge_key(domain, "WORKFLOW_FAILED", "order-settlement", "TIMEOUT")
    assert key == knowledge_key(str(domain), "WORKFLOW_FAILED", "order-settlement", "TIMEOUT")
    assert key != knowledge_key(domain, "WORKFLOW_FAILED", "order-settlement", "OTHER")
    assert knowledge_key(domain, None, None, None) != key
