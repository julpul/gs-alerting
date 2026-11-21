from enum import Enum
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.pipeline.ruleview import cfg


class Sev(Enum):
    error = "error"


def test_reads_json_definitions_in_camel_case():
    rule = {"presentation": {"titleTemplate": "T {{x}}", "fields": ["a"]}, "dedup": {"fields": ["id"]}}
    assert cfg(rule, "presentation.title_template") == "T {{x}}"
    assert cfg(rule, "presentation.fields") == ["a"]
    assert cfg(rule, "dedup.fields") == ["id"]
    assert cfg(rule, "grouping.by_field") is None
    assert cfg(rule, "grouping.by_field", "dflt") == "dflt"


def test_reads_model_objects_and_enums():
    class Presentation(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
        title_template: str | None = None

    rule = SimpleNamespace(
        presentation=Presentation(title_template="x"), priority=Sev.error, grouping=None, runbook_id=None
    )
    assert cfg(rule, "presentation.title_template") == "x"
    assert cfg(rule, "priority") == "error"
    assert cfg(rule, "grouping.by_field") is None
    assert cfg(rule, "runbook_id", 7) == 7


def test_camel_case_attributes_are_a_fallback():
    assert cfg(SimpleNamespace(teamId="t"), "team_id") == "t"
