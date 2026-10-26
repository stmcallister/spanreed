"""The brief archive, and the button that generates one now.

Generate now runs the real composer against today's items. It is the only place
in the UI that spends tokens, so it says so on the button.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from spanreed.compose.pipeline import brief_date_for, compose
from spanreed.deliver import feed
from spanreed.web import repo
from spanreed.web.deps import Config, Conn, templates

log = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/briefs", response_class=HTMLResponse)
def list_briefs(request: Request, conn: Conn, settings: Config) -> Response:
    return templates.TemplateResponse(
        request,
        "briefs/list.html",
        {"briefs": repo.brief_dates(conn), "timezone": settings.render_timezone},
    )


@router.get("/feed.xml")
def brief_feed(request: Request, conn: Conn) -> Response:
    """The archive as RSS.

    Behind the same basic auth as everything else, because it carries the whole
    brief. Readers that support a credential in the URL can subscribe to it.
    """
    return Response(
        content=feed.build(repo.recent_briefs(conn, feed.MAX_FEED_ITEMS), str(request.base_url)),
        media_type="application/rss+xml",
    )


@router.get("/briefs/{brief_date}", response_class=HTMLResponse)
def show_brief(request: Request, brief_date: str, conn: Conn, settings: Config) -> Response:
    brief = repo.get_brief(conn, brief_date)
    if brief is None:
        raise HTTPException(status_code=404, detail=f"no brief for {brief_date}")
    return templates.TemplateResponse(
        request,
        "briefs/detail.html",
        {
            "brief": brief,
            "items": repo.brief_items(conn, brief.id),
            "timezone": settings.render_timezone,
        },
    )


@router.post("/briefs/generate")
def generate_now(conn: Conn, settings: Config) -> Response:
    """Run the composer against today's items, for testing.

    This is the same code path the CronJob takes, degraded paths included, so
    what it produces is what tomorrow morning would have produced.
    """
    now = datetime.now(UTC)
    log.info("generating a brief on request")
    compose(conn, settings, now)
    return RedirectResponse(
        f"/briefs/{brief_date_for(now, settings.render_timezone)}", status_code=303
    )
