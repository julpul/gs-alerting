from app.pipeline.fields import MISSING, EventView, get_path, lookup

DOC = {
    "labels": {"severity": "critical", "n": 0, "none": None},
    "alerts": [{"labels": {"name": "A"}}, {"labels": {"name": "B"}}],
    "a.b": 1,
}


def test_get_path_nested():
    assert get_path(DOC, "labels.severity") == "critical"
    assert get_path(DOC, "labels.n") == 0


def test_get_path_array_index():
    assert get_path(DOC, "alerts.0.labels.name") == "A"
    assert get_path(DOC, "alerts.1.labels.name") == "B"


def test_get_path_missing_is_none():
    assert get_path(DOC, "labels.nope") is None
    assert get_path(DOC, "alerts.5.labels") is None
    assert get_path(DOC, "alerts.x") is None
    assert get_path(DOC, "labels.severity.deeper") is None
    assert get_path(None, "a") is None


def test_lookup_distinguishes_null_from_missing():
    assert lookup(DOC, "labels.none") is None
    assert lookup(DOC, "labels.absent") is MISSING
    assert lookup(DOC, "") is DOC


def test_event_view_meta_and_raw():
    ev = EventView(raw=DOC, meta={"severity": "error", "service": "payments-api"})
    assert ev.get("_severity") == "error"
    assert ev.get("labels.severity") == "critical"
    assert ev.get("_missing") is None
    assert ev.lookup("_missing") is MISSING
    assert ev.lookup("labels.absent") is MISSING
