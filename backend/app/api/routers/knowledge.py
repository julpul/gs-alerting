"""Knowledge base API (spec 12): upload Markdown/text or a URL, list the documents visible to the caller."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Query, Request, Response
from starlette.datastructures import UploadFile

from app.ai.retrieval import KnowledgeScope
from app.api.deps import CurrentScope, Pool
from app.core.errors import AppError, ORJSONResponse, forbidden
from app.core.ids import parse_uuid
from app.core.scope import Scope
from app.knowledge.fetch import MAX_BYTES, fetch_url
from app.knowledge.indexer import DOC_COLUMNS, doc_wire, index_document
from app.repositories import rules as rules_repo

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])

TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".text", ".rst", ".log")


async def _read_input(request: Request) -> dict[str, Any]:
    """Normalise a multipart form or a JSON body into `{content, url, title, teamId, domainId, filename}`."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError as exc:
            raise AppError(422, "invalid_body", "invalid JSON body") from exc
        if not isinstance(body, dict):
            raise AppError(422, "invalid_body", "body must be an object")
        return {k: body.get(k) for k in ("content", "url", "title", "teamId", "domainId")}
    form = await request.form()
    out: dict[str, Any] = {k: form.get(k) or None for k in ("url", "title", "teamId", "domainId")}
    upload = form.get("file")
    if isinstance(upload, UploadFile):
        data = await upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise AppError(422, "too_large", "the document is larger than 1 MB")
        name = (upload.filename or "").lower()
        if not (upload.content_type or "").startswith("text/") and not name.endswith(TEXT_SUFFIXES):
            raise AppError(422, "unsupported_type", "upload Markdown or plain text (or give a URL)")
        out["content"] = data.decode("utf-8", errors="replace")
        out["filename"] = upload.filename
    return out


async def _resolve_domain(conn: asyncpg.Connection, ref: str | None) -> UUID | None:
    if not ref:
        return None
    ident = parse_uuid(ref)
    return await conn.fetchval(
        "SELECT id FROM domains WHERE id = $1 OR slug = $2", ident or UUID(int=0), str(ref)
    )


async def _target(
    conn: asyncpg.Connection, scope: Scope, team_ref: str | None, domain_ref: str | None
) -> tuple[UUID, UUID | None]:
    """(domain_id, team_id) the caller may write to; 403/422 otherwise."""
    domain_id = await _resolve_domain(conn, domain_ref)
    if domain_ref and domain_id is None:
        raise AppError(422, "invalid_domain", "unknown domain")
    if team_ref:
        team_id = await rules_repo.resolve_team(conn, team_ref)
        if team_id is None:
            raise AppError(422, "invalid_team", "unknown team")
        if not await rules_repo.can_edit_team(conn, scope, team_id):
            raise forbidden("you cannot add knowledge for this team")
        linked = [
            r["domain_id"]
            for r in await conn.fetch(
                "SELECT td.domain_id FROM team_domains td JOIN domains d ON d.id = td.domain_id "
                "WHERE td.team_id = $1 ORDER BY d.slug",
                team_id,
            )
        ]
        if domain_id is not None and domain_id not in linked:
            raise AppError(422, "invalid_domain", "the team does not belong to this domain")
        if domain_id is None and not linked:
            raise AppError(422, "invalid_team", "the team has no domain")
        return domain_id or linked[0], team_id
    if domain_id is None:
        roots = [r.scope_id for r in scope.roles if r.role == "domain_root" and r.scope_type == "domain"]
        if len(roots) != 1:
            raise AppError(422, "invalid_target", "give teamId or domainId")
        domain_id = roots[0]
    is_root = any(
        r.role == "domain_root" and r.scope_type == "domain" and r.scope_id == domain_id for r in scope.roles
    )
    if not (scope.is_master or is_root):
        raise forbidden("only a domain root can add domain-wide knowledge")
    return domain_id, None


_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FRONT_TITLE = re.compile(r"^title[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
_H1 = re.compile(r"^[ \t]{0,3}#[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,3}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)


def title_from(text: str) -> str | None:
    """Front-matter `title:`, else the first `# heading` (else the first `##`/`###`), else None."""
    front = _FRONT_MATTER.match(text)
    if front:
        found = _FRONT_TITLE.search(front.group(1))
        if found and found.group(1).strip("'\" "):
            return found.group(1).strip().strip("'\"").strip()[:300]
        text = text[front.end() :]
    match = _H1.search(text) or _HEADING.search(text)
    return match.group(1).strip()[:300] if match else None


def _file_title(filename: str | None) -> str | None:
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return stem or None


@router.get("")
async def list_knowledge(
    pool: Pool, scope: CurrentScope, limit: int = Query(200, ge=1, le=500)
) -> ORJSONResponse:
    where, args = KnowledgeScope.of_user(scope).where(1)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {DOC_COLUMNS} FROM knowledge_docs d WHERE {where} "
            f"ORDER BY d.created_at DESC, d.id DESC LIMIT {limit}",
            *args,
        )
    return ORJSONResponse([doc_wire(r) for r in rows])


@router.post("", status_code=201)
async def add_knowledge(request: Request, pool: Pool, scope: CurrentScope) -> Response:
    data = await _read_input(request)
    content = (data.get("content") or "").strip()
    url = (data.get("url") or "").strip() or None
    if not content and not url:
        raise AppError(422, "invalid_body", "give a file, a text body or a URL")
    async with pool.acquire() as conn:
        domain_id, team_id = await _target(conn, scope, data.get("teamId"), data.get("domainId"))
        title = (data.get("title") or "").strip() or None
        kind = "upload"
        if not content and url:
            fetched_title, content = await fetch_url(url)
            title = title or fetched_title
            kind = "link"
        if not content.strip():
            raise AppError(422, "invalid_body", "the document is empty")
        row = await index_document(
            conn,
            domain_id=domain_id,
            team_id=team_id,
            kind=kind,
            title=title or title_from(content) or _file_title(data.get("filename")) or "Untitled document",
            text=content,
            url=url,
        )
    return ORJSONResponse(doc_wire(row), status_code=201)
