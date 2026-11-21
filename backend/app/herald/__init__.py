"""Herald UI backend-for-frontend (BFF).

The web UI (backend/static/herald, sources in ui/) was written against the `/api/*` shapes of the
Herald prototype. This package serves exactly those shapes on top of our services (`/v1` stays the
public contract). Every handler reuses our repositories/service functions; no business logic lives here,
only identity mapping and JSON shape translation.
"""
