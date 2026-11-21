from app.pipeline.schema_detect import detect_schema


def test_meta_schema_wins():
    raw = {"meta": {"schema": "trade.rejected.v2"}, "type": "X", "alertname": "Y"}
    assert detect_schema(raw) == "trade.rejected.v2"


def test_type_then_alertname_then_schema():
    assert detect_schema({"type": "WORKFLOW_FAILED", "alertname": "Y"}) == "WORKFLOW_FAILED"
    assert detect_schema({"alertname": "HighErrorRate", "schema": "s"}) == "HighErrorRate"
    assert detect_schema({"schema": "custom.v1"}) == "custom.v1"


def test_non_string_or_blank_values_are_skipped():
    assert detect_schema({"type": 5, "alertname": "  ", "schema": "s"}) == "s"
    assert detect_schema({"meta": {"schema": None}, "type": "T"}) == "T"
    assert detect_schema({"meta": "not-a-dict", "type": "T"}) == "T"


def test_key_set_hash_is_stable_and_order_independent():
    a = detect_schema({"level": "ERROR", "logger": "x", "message": "m"})
    b = detect_schema({"message": "other", "logger": "y", "level": "WARN"})
    assert a == b
    assert a.startswith("keys:") and len(a) == len("keys:") + 8
    assert detect_schema({"level": "ERROR"}) != a
