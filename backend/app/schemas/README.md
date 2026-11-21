# Schemas

- `api.py` — **generated** from `docs/openapi.yaml` (pydantic v2). Do not edit by hand; field names are the
  contract's camelCase names, so the wire format needs no aliases.
- `internal.py` — hand-written internal models (`User`, `RoleAssignment`, `CamelModel` base for new wire models).

Regenerate after any change to the contract (run from `backend/`):

```bash
uv run datamodel-codegen \
  --input ../docs/openapi.yaml --input-file-type openapi \
  --output app/schemas/api.py \
  --output-model-type pydantic_v2.BaseModel \
  --use-annotated --field-constraints --target-python-version 3.12
```

`api.py` is excluded from `ruff` (see `pyproject.toml`). Inline request/response bodies of the contract
(for example the login payload) are not generated; routers declare small models for them.
