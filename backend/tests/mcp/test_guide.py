"""The bundled OpenAPI copy and the integration guide."""

from __future__ import annotations

from pathlib import Path

from app.mcp.guide import OPENAPI_PATH, docs_resource, endpoint_index, guide_markdown

REPO_DOCS = Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml"


def test_bundled_openapi_is_identical_to_the_contract():
    assert OPENAPI_PATH.read_text() == REPO_DOCS.read_text(), (
        "app/mcp/static/openapi.yaml is stale: cp docs/openapi.yaml backend/app/mcp/static/openapi.yaml"
    )


def test_guide_has_curl_python_and_mcp_snippets():
    guide = guide_markdown()
    assert "curl -X POST" in guide and "httpx.post" in guide
    assert "claude mcp add --transport http alerting" in guide and "/mcp" in guide
    assert "<SOURCE_KEY>" in guide
    resource = docs_resource()
    assert guide in resource and "openapi: 3.1.0" in resource


def test_endpoint_index_lists_the_contract_paths():
    index = endpoint_index()
    assert any(e["path"] == "/v1/alerts/raise" and e["method"] == "POST" for e in index)
    assert any(e["path"] == "/v1/agent/chat" for e in index)
