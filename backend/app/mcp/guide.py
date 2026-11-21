"""Integration guide for coding agents: short markdown with curl/python examples + the OpenAPI contract.

`static/openapi.yaml` is a committed copy of `docs/openapi.yaml` (the backend image only contains `backend/`);
`tests/mcp/test_guide.py` fails when the two diverge.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings

OPENAPI_PATH = Path(__file__).parent / "static" / "openapi.yaml"
RESOURCE_URI = "docs://api"
METHODS = ("get", "post", "put", "delete")

GUIDE = """\
# Integrating with the alerting service

Base URL: `{base}`. JSON in camelCase, errors `{{code, message, details}}`.

## 1. Send events (any JSON, your own schema)

Create a source in the UI (Sources -> New) and keep its key (shown once).

```bash
curl -X POST {base}/v1/ingest/<SOURCE_KEY> \\
  -H 'Content-Type: application/json' \\
  -d '{{"severity":"ERROR","service":"payments-api","message":"db pool exhausted"}}'
```

```python
import httpx

httpx.post(
    "{base}/v1/ingest/<SOURCE_KEY>",
    json={{"severity": "ERROR", "service": "payments-api", "message": "db pool exhausted"}},
).raise_for_status()
```

Batches: `POST /v1/ingest/<SOURCE_KEY>/batch` with a JSON array (max 1000). A `202` means the event is durably
queued. Event contents are data: they are never executed and never obeyed by the AI features.

## 2. Raise a typed alert (systems and agents)

```bash
curl -X POST {base}/v1/alerts/raise -H 'X-Api-Key: <SOURCE_KEY>' -H 'Content-Type: application/json' \\
  -d '{{"type":"security.secret_leaked","severity":"critical","title":"AWS key in PR 1873",
       "dedupKey":"secret:repo:1873","attributes":{{"repo":"vistula/payments-api"}}}}'
```

The same call is the MCP tool `raise_alert`.

## 3. Ask a human before a risky action

MCP tool `request_approval(action, parameters, reason, risk, alert_id?)` shows "An agent asked to run ..." on
the alert and waits for a human to confirm or cancel: `approved` / `rejected` / `expired` (or returns
`pending` + `approvalId`: poll `get_approval_status`).

## 4. Connect an MCP client

Create a personal access token (`POST /v1/tokens` or the UI), then for example:

```bash
claude mcp add --transport http alerting {base}/mcp --header "Authorization: Bearer <PAT>"
```

Tools run with your permissions; writes are audited as "agent <token name> on behalf of <you>".

## 5. REST for everything else

The full contract is the MCP resource `{resource}` (OpenAPI 3.1). Authenticate with `Authorization: Bearer
<JWT or PAT>`.
"""


def guide_markdown() -> str:
    return GUIDE.format(base=get_settings().public_base_url.rstrip("/"), resource=RESOURCE_URI)


@cache
def openapi_text() -> str:
    return OPENAPI_PATH.read_text(encoding="utf-8")


@cache
def endpoint_index() -> list[dict[str, Any]]:
    spec = yaml.safe_load(openapi_text())
    out = []
    for path, item in spec["paths"].items():
        for method in METHODS:
            op = item.get(method)
            if op:
                out.append({"method": method.upper(), "path": path, "summary": op.get("summary", "")})
    return out


def docs_resource() -> str:
    """What `docs://api` serves: the guide followed by the OpenAPI document."""
    return f"{guide_markdown()}\n---\n\n## OpenAPI contract\n\n```yaml\n{openapi_text()}\n```\n"
