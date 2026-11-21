"""Entry point: `python -m app.main [--role api|worker|delivery|scheduler|ai|connector|all|a,b,...]`.

The role defaults to `$APP_ROLE` (then `api`). The API role runs uvicorn on `$PORT` (default 8080);
other roles run their `async def run(stop)` concurrently in the same event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterator

import uvicorn

from app.config import get_settings
from app.core.logging import setup_logging
from app.core.stats import run_stats_mirror, start_metrics_server
from app.roles import API_ROLE, load_runner, parse_roles

log = logging.getLogger("app.main")


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to `serve_roles`."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve_roles(roles: list[str], stop: asyncio.Event | None = None) -> None:
    """Run the given roles until `stop` is set (SIGTERM/SIGINT set it) or a role exits/crashes."""
    settings = get_settings()
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    tasks: dict[asyncio.Task, str] = {}
    server: uvicorn.Server | None = None
    if API_ROLE in roles:
        config = uvicorn.Config(
            "app.api.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=settings.port,
            log_config=None,
            access_log=False,
        )
        server = _Server(config)
        tasks[asyncio.create_task(server.serve(), name=API_ROLE)] = API_ROLE
    for role in roles:
        if role != API_ROLE:
            tasks[asyncio.create_task(load_runner(role)(stop), name=role)] = role
    if API_ROLE not in roles:
        start_metrics_server(settings.metrics_port)
    mirror = asyncio.create_task(run_stats_mirror(stop), name="stats-mirror")
    log.info("started roles: %s", ", ".join(roles))

    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    if server is not None:
        server.should_exit = True
    stopper.cancel()
    mirror.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [
        (tasks_role, r)
        for (tasks_role, r) in zip(tasks.values(), results, strict=True)
        if isinstance(r, Exception)
    ]
    for role, exc in failures:
        log.error("role %s failed: %r", role, exc)
    if failures:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="gs-alerting backend")
    parser.add_argument(
        "--role",
        default=settings.app_role,
        help="api|worker|delivery|scheduler|ai|connector|all or a comma list",
    )
    args = parser.parse_args(argv)
    setup_logging(settings.log_level)
    roles = parse_roles(args.role)
    asyncio.run(serve_roles(roles))


if __name__ == "__main__":
    main()
