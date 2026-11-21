import sys

import pytest
from fastapi import APIRouter

from app.api.app import create_app, discover_routers


def test_discovery_finds_real_routers():
    routers = discover_routers()
    paths = {route.path for r in routers for route in r.routes}
    assert {"/healthz", "/readyz", "/v1/auth/login", "/v1/auth/me"} <= paths


def test_create_app_includes_discovered_routes():
    app = create_app()
    paths = {route.path for route in app.router.routes if hasattr(route, "path")}
    paths |= set(app.openapi()["paths"])
    assert {"/healthz", "/readyz", "/v1/auth/login", "/v1/auth/me", "/metrics"} <= paths


def test_discovery_only_picks_modules_with_a_router(tmp_path, monkeypatch):
    pkg = tmp_path / "fake_routers"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/alpha')\n"
        "@router.get('/ping')\nasync def ping():\n    return {'ok': True}\n"
    )
    (pkg / "helper.py").write_text("router = 'not a router'\nVALUE = 1\n")
    (pkg / "beta.py").write_text("from fastapi import APIRouter\nrouter = APIRouter(prefix='/beta')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        found = discover_routers("fake_routers")
    finally:
        for name in [m for m in sys.modules if m.startswith("fake_routers")]:
            del sys.modules[name]
    assert [r.prefix for r in found] == ["/alpha", "/beta"]
    assert all(isinstance(r, APIRouter) for r in found)


def test_discovery_of_missing_package_raises():
    with pytest.raises(ModuleNotFoundError):
        discover_routers("no_such_routers_pkg")
