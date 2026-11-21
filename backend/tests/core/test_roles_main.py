import asyncio

import pytest

from app.main import serve_roles
from app.roles import ALL_ROLES, load_runner, parse_roles


def test_parse_single_and_list():
    assert parse_roles("api") == ["api"]
    assert parse_roles("worker,delivery, scheduler ,ai") == ["worker", "delivery", "scheduler", "ai"]
    assert parse_roles("worker,worker") == ["worker"]


def test_parse_all_includes_every_role():
    assert parse_roles("all") == list(ALL_ROLES)
    assert "api" in parse_roles("all")


@pytest.mark.parametrize("bad", ["", "nope", "worker,bogus"])
def test_parse_rejects_unknown(bad):
    with pytest.raises(ValueError):
        parse_roles(bad)


@pytest.mark.parametrize("role", [r for r in ALL_ROLES if r != "api"])
async def test_stub_role_waits_for_stop(role):
    stop = asyncio.Event()
    task = asyncio.create_task(load_runner(role)(stop))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_serve_roles_runs_until_stop():
    stop = asyncio.Event()
    task = asyncio.create_task(serve_roles(["worker", "delivery", "scheduler", "ai", "connector"], stop))
    await asyncio.sleep(0.1)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_serve_roles_exits_nonzero_when_a_role_crashes(monkeypatch):
    async def boom(stop):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.main.load_runner", lambda role: boom)
    with pytest.raises(SystemExit) as exc:
        await asyncio.wait_for(serve_roles(["worker"], asyncio.Event()), 2)
    assert exc.value.code == 1
