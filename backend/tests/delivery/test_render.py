"""Message rendering: SMS format and length, email HTML escaping, links."""

from __future__ import annotations

from uuid import uuid4

from app.delivery.channels.base import AlertView, Recipient
from app.delivery.channels.registry import adapters, get_adapter
from app.delivery.render import render_email, render_phone, render_sms


def view(**kw) -> AlertView:
    base = dict(
        id=uuid4(),
        short_id=1234,
        title="Settlement failed validate-counterparty",
        body="",
        severity="critical",
        status="open",
        count=1,
        escalation_level=1,
        team_id=uuid4(),
    )
    return AlertView(**{**base, **kw})


def test_sms_matches_spec_format():
    text = render_sms(view(), "hard")
    assert text.startswith("[CRIT] A-1234 Settlement failed validate-counterparty")
    assert "Reply ACK 1234." in text and text.endswith("/a/1234") and len(text) <= 160


def test_sms_is_truncated_to_160_chars_keeping_link():
    text = render_sms(view(title="x" * 500), "hard")
    assert len(text) == 160 and "..." in text and text.endswith("/a/1234")
    assert len(render_sms(view(title="y" * 500), "soft")) <= 160


def test_sms_soft_has_no_ack_instruction_and_tags_follow_state():
    assert "Reply ACK" not in render_sms(view(), "soft")
    assert render_sms(view(status="resolved"), "soft").startswith("[RESOLVED]")
    assert render_sms(view(escalation_level=2), "hard").startswith("[CRIT L2]")
    assert render_sms(view(severity="warning"), "hard").startswith("[WARN]")


def test_phone_text_offers_press_one():
    assert "Press 1 to acknowledge" in render_phone(view())


def test_email_escapes_untrusted_content_and_has_links():
    alert = view(
        title="<script>alert(1)</script>",
        body="<img src=x onerror=1>",
        fields={"step": "<b>bold</b>"},
        runbook_name="settlement-timeout",
        runbook_steps=["Check <b>queue</b>", "Retry"],
    )
    subject, text, html = render_email(alert, Recipient("Anna", "a@x"), "hard")
    assert "<script>" not in html and "&lt;script&gt;" in html and "<img" not in html
    assert "&lt;b&gt;queue&lt;/b&gt;" in html and "settlement-timeout" in html
    assert "?action=ack" in html and "?action=resolve" in html and "?action=ack" in text
    assert subject.startswith("[CRIT] A-1234")
    assert "1. Check <b>queue</b>" in text  # plain text is not HTML


def test_email_soft_has_no_ack_link_and_placeholder_runbook():
    _, text, html = render_email(view(), Recipient("M", "m@x"), "soft")
    assert "action=ack" not in html and "action=ack" not in text
    assert "none attached yet" in html and "none attached yet" in text


def test_registry_has_all_builtin_channels_and_override():
    assert set(adapters()) >= {"ui", "email", "sms", "phone", "webhook"}
    import pytest

    with pytest.raises(KeyError):
        get_adapter("pigeon")
    from app.delivery.channels import registry

    restore = registry.override("ui", object())
    assert get_adapter("ui") is not adapters()["email"]
    restore()
    assert get_adapter("ui").name == "ui"
    restore_new = registry.override("pigeon", object())
    restore_new()
    assert "pigeon" not in adapters()


def test_adapters_render_through_the_adapter_interface():
    alert = view()
    rcpt = Recipient("Piotr", "+48500100201")
    assert get_adapter("sms").render(alert, rcpt, "hard").to == "+48500100201"
    assert get_adapter("phone").render(alert, rcpt, "hard").channel == "phone"
    assert get_adapter("ui").render(alert, Recipient("team", None), "soft").channel == "ui"
    hook = get_adapter("webhook").render(alert, Recipient("hook", "http://x", secret="s"), "soft")
    assert hook.meta["payload"]["alert"]["shortId"] == "A-1234" and hook.meta["secret"] == "s"
