"""TEST-SCENARIOS §4 RB1-RB8 (unseen runbooks) through the deterministic parsers (no LLM): every sample
displays, and the ones with steps in them become >= 2 sensible steps."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runbooks.parsers import detect_format, parse_runbook

RUNBOOKS = Path(__file__).resolve().parents[1] / "fixtures" / "runbooks"


async def parsed(name: str):
    return await parse_runbook((RUNBOOKS / name).read_text(), "auto", use_ai=False)


@pytest.mark.parametrize(
    ("name", "minimum"),
    [
        ("rb1_checklist.md", 5),
        ("rb2_nested.txt", 3),
        ("rb3_table.md", 3),
        ("rb4_link.txt", 1),
        ("rb5_shell.sh", 4),
        ("rb6_tasks.json", 4),
        ("rb7_chat.txt", 2),
        ("rb8_spanish.txt", 1),
    ],
)
async def test_every_sample_parses_into_steps(name, minimum):
    runbook = await parsed(name)
    assert len(runbook.steps) >= minimum
    assert all(step.text and step.text.strip() for step in runbook.steps)
    assert [s.order for s in runbook.steps] == list(range(1, len(runbook.steps) + 1))


async def test_rb1_sign_off_means_approval():
    steps = (await parsed("rb1_checklist.md")).steps
    assert [s.approval.value for s in steps].count("required") == 1
    assert "{{labels.service}}" in steps[0].text


async def test_rb3_table_rows_become_steps_without_header_or_separator():
    runbook = await parsed("rb3_table.md")
    assert detect_format((RUNBOOKS / "rb3_table.md").read_text()) == "markdown"
    texts = [s.text for s in runbook.steps]
    assert len(texts) == 3
    assert not any("---" in t or t.startswith("Symptom: Symptom") for t in texts)
    assert texts[0].startswith("Symptom: Errors in one region only; Likely cause: Regional outage")
    assert [s.approval.value for s in runbook.steps] == ["required", "required", "none"]
    assert [s.action for s in runbook.steps] == ["failover_region", "rollback_deploy", "open_ticket"]


async def test_markdown_page_with_items_and_a_table_keeps_document_order():
    content = "# Page\n- First do this\n\n| Step | Who |\n|---|---|\n| Second | ops |\n\n- Third\n"
    runbook = await parse_runbook(content, use_ai=False)
    assert [s.text for s in runbook.steps] == ["First do this", "Step: Second; Who: ops", "Third"]


async def test_rb5_shell_is_read_only_steps():
    steps = (await parsed("rb5_shell.sh")).steps
    assert not any(s.text.startswith("#!") for s in steps)
    assert any("`sleep 120`" in s.text for s in steps)
    assert any(s.action == "restart_service" and s.suggested for s in steps)


async def test_rb6_alias_vocabulary():
    runbook = await parsed("rb6_tasks.json")
    assert runbook.name == "fix-session-drop"
    steps = runbook.steps
    assert [s.kind.value for s in steps] == ["text", "action", "text", "text"]
    assert steps[1].action == "restart_service"
    assert steps[1].approval.value == "required"
    assert steps[1].parameters["service"] == "{{component}}"
    assert steps[2].text == "Wait 60 seconds"
    assert "page_duty_manager" in steps[3].text
    assert steps[3].parameters["unregisteredAction"] == "page_duty_manager"
    assert steps[3].action is None


@pytest.mark.parametrize(
    "content",
    [
        '{"procedure": ["Check the dashboard", "Restart the gateway"]}',
        "actions:\n  - title: Check the dashboard\n  - description: Page the on-call\n",
        '[{"name": "Check it"}, {"command": "kubectl get pods"}]',
    ],
)
async def test_other_step_list_keys(content):
    runbook = await parse_runbook(content, use_ai=False)
    assert runbook.format == "yaml" and len(runbook.steps) == 2


async def test_unknown_json_is_one_formatted_step():
    content = '{"owner": "pricing", "notes": {"a": 1}}'
    assert detect_format(content) == "json"
    runbook = await parse_runbook(content, use_ai=False)
    assert len(runbook.steps) == 1 and '"owner": "pricing"' in runbook.steps[0].text
