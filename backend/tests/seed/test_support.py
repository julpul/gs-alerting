import itertools
from datetime import timedelta
from importlib import import_module

import pytest

from seed.loaders._support import chunk_text, parse_offset, profile, replace_placeholders


def test_parse_offset_forms():
    assert parse_offset("-12d3h") == -timedelta(days=12, hours=3)
    assert parse_offset("1h35m") == timedelta(hours=1, minutes=35)
    assert parse_offset("0s") == timedelta(0)
    assert parse_offset("52m") == timedelta(minutes=52)
    for bad in ("", "soon", "-"):
        with pytest.raises(ValueError):
            parse_offset(bad)


def test_chunk_text_sizes_and_overlap():
    paragraph = "Counterparty service ops. " * 12
    text = "\n\n".join(f"{i}. {paragraph}" for i in range(8))
    chunks = chunk_text(text)
    assert len(chunks) > 3
    assert all(0 < len(c) <= 800 for c in chunks)
    # consecutive chunks overlap: the head of the next one occurs inside the previous one
    assert all(nxt[:30] in prev for prev, nxt in itertools.pairwise(chunks))
    assert chunk_text("short") == ["short"]
    assert chunk_text("   ") == []


def test_replace_placeholders_is_recursive():
    raw = {"ts": "@openedAt", "meta": {"ts": "@openedAt:epoch_ms"}, "list": ["@openedAt", "x"], "n": 3}
    assert replace_placeholders(raw, "2026-01-01T00:00:00.000Z", 123) == {
        "ts": "2026-01-01T00:00:00.000Z",
        "meta": {"ts": 123},
        "list": ["2026-01-01T00:00:00.000Z", "x"],
        "n": 3,
    }


def test_profile_defaults_to_full(monkeypatch):
    monkeypatch.delenv("SEED_PROFILE", raising=False)
    assert profile() == "full"
    monkeypatch.setenv("SEED_PROFILE", "CORE")
    assert profile() == "core"


def test_working_hours_json_shape_matches_channel_select():
    org_loader = import_module("seed.loaders.10_org")
    stored = org_loader.working_hours_json({"start": "08:30", "end": "18:30", "days": [1]}, "Europe/London")
    assert stored == {"from": "08:30", "to": "18:30", "timezone": "Europe/London"}
    assert org_loader.working_hours_json(None, "Europe/Warsaw") == {}
