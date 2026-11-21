"""Herald UI: `POST /api/alerts/{id}/codebase/pr` opens a draft pull request carrying the incident brief."""

from __future__ import annotations

import logging
from typing import Any

from app.api.deps import Pool
from app.core.audit import audit
from app.core.errors import AppError, not_found
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.herald import codebase as cb
from app.herald import github
from app.herald.identity import Actor
from app.herald.ids import short_from_ref
from app.herald.routing import herald_router
from app.repositories import alert_actions as actions
from app.repositories import alerts as repo

log = logging.getLogger(__name__)
router = herald_router(prefix="/api/alerts", tags=["herald"])


@router.post("/{alert_id}/codebase/pr")
async def open_pr(alert_id: str, who: Actor, pool: Pool) -> dict[str, Any]:
    short = short_from_ref(alert_id)
    if short is None:
        raise not_found("alert")
    async with pool.acquire() as conn, conn.transaction():
        row = await repo.find_alert(conn, f"A-{short}", who.scope, for_update=True, detail=True)
        if row is None:
            raise AppError(404, "not_found", "no such alert")
        await actions.require_actor_role(conn, who.scope, row)
        bundle = await cb.codebase_bundle(conn, row, await repo.alert_detail(conn, row))
        codebase = bundle["codebase"]
        if codebase is None:
            raise AppError(409, "conflict", "this platform has no codebase linked")
        pr = codebase["pr"]
        if pr["url"]:
            opened = await cb.pr_url_for(conn, row["id"])
            return {"url": opened["url"], "number": opened.get("number"), "branch": opened.get("branch")}
        if not pr["available"]:
            raise AppError(409, "conflict", pr["reason"])
        owner, name = cb.github_slug(codebase["repo"]) or ("", "")
        alert = dict(row)
        key = f"A-{short}"
        brief_path = "/".join(p for p in (codebase["path"], ".herald", f"{key}.md") if p)
        name_of = who.user["display_name"]
        brief = cb.brief_markdown(alert, codebase["claude_prompt"])
        result = await github.open_draft_pr(
            owner,
            name,
            base=codebase["branch"],
            branch=f"herald/a-{short}-{github.slugify(row['title'])}",
            file_path=brief_path,
            file_content=brief,
            title=f"{key}: {row['title']} (draft fix)",
            body=f"{brief}\nOpened from Herald by {name_of}.",
        )
        await add_timeline(
            conn,
            row["id"],
            "pr_opened",
            f"Draft pull request opened by {name_of}",
            actor=who.actor,
            data=result,
        )
        await audit(conn, who.actor, "alert.pr_opened", "alert", str(row["id"]), result)
        summary = repo.summary_out(await repo.find_alert(conn, str(row["id"]), who.scope))
    try:
        await publish_sse("alert.updated", summary, team_ids=[row["team_id"]])
    except Exception:
        log.exception("SSE publish of alert.updated failed")
    return result
