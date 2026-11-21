"""`{{path}}` rendering: lookups, escaping, truncation, no code execution."""

from app.pipeline.fields import EventView
from app.pipeline.rules.templates import render

EV = EventView(
    raw={
        "payload": {"currency": "EUR", "notional": 25000000, "reason": "LIMIT_BREACH", "ratio": 2.0},
        "items": [{"id": "x1"}],
        "html": '<script>alert(1)</script> & "q"',
        "flag": True,
        "nothing": None,
        "obj": {"a": 1},
    },
    meta={"severity": "error", "service": "trade-gateway"},
)


def test_basic_substitution_with_meta_and_array_index():
    assert (
        render("Rejected {{payload.currency}} {{payload.notional}} — {{payload.reason}}", EV)
        == "Rejected EUR 25000000 — LIMIT_BREACH"
    )
    assert render("{{_severity}}/{{_service}}", EV) == "error/trade-gateway"
    assert render("{{items.0.id}}", EV) == "x1"
    assert render("{{ payload.currency }}", EV) == "EUR"  # whitespace allowed


def test_values_are_stringified_predictably():
    assert render("{{flag}}|{{nothing}}|{{payload.ratio}}|{{obj}}", EV) == 'true||2|{"a":1}'


def test_missing_paths_render_empty():
    assert render("[{{nope}}][{{payload.nope.deeper}}][{{_missing}}][{{items.5.id}}]", EV) == "[][][][]"


def test_html_escape_applies_to_values_only():
    assert (
        render("<b>{{html}}</b>", EV, html=True)
        == "<b>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;</b>"
    )
    assert render("{{html}}", EV) == EV.raw["html"]


def test_truncation_caps_total_length_with_ellipsis():
    ev = EventView({"v": "x" * 2000})
    out = render("a {{v}} b", ev)
    assert len(out) == 500 and out.endswith("…") and out.startswith("a xxx")
    assert render("{{v}}", ev, max_len=10) == "x" * 9 + "…"
    assert render("short", ev, max_len=10) == "short"
    assert render("exactly10!", ev, max_len=10) == "exactly10!"
    assert render("abc", ev, max_len=0) == ""


def test_truncation_never_cuts_an_html_entity():
    ev = EventView({"v": "&" * 100})
    out = render("{{v}}", ev, html=True, max_len=11)
    assert out == "&amp;" * 10 + "…"


def test_template_syntax_is_inert():
    dangerous = EventView({"__class__": "x", "a": "{{b}}", "b": "SECRET"})
    # dunder lookups are plain dict keys; unknown ones are empty
    assert render("{{__class__.__mro__}}", dangerous) == ""
    assert render("{{ ''.__class__ }}", dangerous) == ""
    assert render("{{__import__('os').system('id')}}", dangerous) == ""
    # values are never re-expanded
    assert render("{{a}}", dangerous) == "{{b}}"
    assert (
        render("{% for x in y %}{{b}}{% endfor %} ${b} {b}", dangerous)
        == "{% for x in y %}SECRET{% endfor %} ${b} {b}"
    )
    # nested/unbalanced braces
    assert render("{{{{b}}}}", dangerous) == "{{SECRET}}"
    assert render("{{b", dangerous) == "{{b"
