"""Hand-written internal models (the wire contract lives in the generated `app.schemas.api`)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

Role = Literal["master", "domain_root", "member", "informed"]
ScopeType = Literal["org", "domain", "team"]


class CamelModel(BaseModel):
    """Base for models that cross the wire: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class User(BaseModel):
    id: UUID
    email: str
    display_name: str
    phone: str | None = None
    manager_id: UUID | None = None
    timezone: str = "Europe/Warsaw"


class RoleAssignment(BaseModel):
    role: Role
    scope_type: ScopeType
    scope_id: UUID
