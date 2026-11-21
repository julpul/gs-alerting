"""RuleIndex: candidates (indexed + wildcard), reloads, disabled/invalid rules."""

from __future__ import annotations

from app.pipeline.rules.index import RuleIndex

from .conftest import cond, definition, view


def ids(candidates) -> set:
    return {c.rule.id for c in candidates}


async def test_candidates_are_index_hits_plus_wildcards(seeded, insert_rule, unique):
    by_source = await insert_rule(definition(cond("_source", "eq", f"src-{unique}"), cond("x", "gt", 1)))
    by_schema = await insert_rule(
        definition(cond("level", "eq", "ERROR"), cond("_schema", "eq", f"sch-{unique}"))
    )
    by_type = await insert_rule(definition(cond("_type", "eq", f"typ-{unique}")))
    wildcard = await insert_rule(definition(cond("level", "eq", "ERROR")))
    glob_only = await insert_rule(definition(cond("_schema", "glob", f"sch-{unique}*")))
    or_group = await insert_rule(
        definition(cond("_source", "eq", f"src-{unique}"), cond("level", "eq", "X"), op="or")
    )
    disabled = await insert_rule(definition(cond("_source", "eq", f"src-{unique}")), enabled=False)

    index = RuleIndex()
    await index.load_all(seeded)

    got = ids(index.candidates(view({}, source=f"src-{unique}")))
    assert by_source in got and wildcard in got and glob_only in got and or_group in got
    assert by_schema not in got and by_type not in got and disabled not in got

    got = ids(index.candidates(view({}, schema=f"sch-{unique}", type=f"typ-{unique}")))
    assert {by_schema, by_type, wildcard, glob_only, or_group} <= got
    assert by_source not in got

    got = ids(index.candidates(view({})))
    assert wildcard in got and by_source not in got and by_schema not in got and by_type not in got
    assert disabled not in ids(index.candidates(view({}, source=f"src-{unique}")))


async def test_indexed_rule_is_filtered_by_its_other_conditions(seeded, insert_rule, unique):
    rule_id = await insert_rule(definition(cond("_source", "eq", f"src-{unique}"), cond("x", "gt", 10)))
    index = RuleIndex()
    await index.load_all(seeded)
    low, high = view({"x": 1}, source=f"src-{unique}"), view({"x": 11}, source=f"src-{unique}")
    assert [c.rule.id for c in index.candidates(low) if c.matches(low)] == []
    assert [c.rule.id for c in index.candidates(high) if c.matches(high) and c.rule.id == rule_id] == [
        rule_id
    ]


async def test_reload_rule_adds_updates_and_removes(seeded, insert_rule, unique):
    index = RuleIndex()
    await index.load_all(seeded)
    rule_id = await insert_rule(definition(cond("_source", "eq", f"old-{unique}")))
    assert index.get(rule_id) is None

    await index.reload_rule(seeded, rule_id)
    assert rule_id in ids(index.candidates(view({}, source=f"old-{unique}")))

    new_def = definition(cond("_source", "eq", f"new-{unique}"))
    await seeded.execute("UPDATE rules SET definition = $2, version = 2 WHERE id = $1", rule_id, new_def)
    await index.reload_rule(seeded, rule_id)
    assert rule_id not in ids(index.candidates(view({}, source=f"old-{unique}")))
    assert rule_id in ids(index.candidates(view({}, source=f"new-{unique}")))
    assert index.get(rule_id).rule.version == 2

    await seeded.execute("UPDATE rules SET enabled = false WHERE id = $1", rule_id)
    await index.reload_rule(seeded, rule_id)
    assert index.get(rule_id) is None

    await seeded.execute("UPDATE rules SET enabled = true WHERE id = $1", rule_id)
    await index.reload_rule(seeded, rule_id)
    assert index.get(rule_id) is not None
    await seeded.execute("DELETE FROM rules WHERE id = $1", rule_id)
    await index.reload_rule(seeded, rule_id)
    assert index.get(rule_id) is None


async def test_invalid_stored_rule_is_skipped_not_fatal(seeded, insert_rule, unique):
    bad = await insert_rule(definition(cond("x", "regex", "(a+)+")))
    good = await insert_rule(definition(cond("x", "eq", unique)))
    index = RuleIndex()
    await index.load_all(seeded)
    assert index.get(bad) is None and index.get(good) is not None
    await index.reload_rule(seeded, bad)
    assert index.get(bad) is None
    assert len(index) >= 1
