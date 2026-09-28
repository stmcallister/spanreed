"""Source health, and the readiness probe.

Sources sorted worst first, with anything the collector auto disabled pinned to
the top. A source that quietly stopped working is the failure mode this screen
exists to make loud.
"""

from __future__ import annotations

import sqlite3

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from newsbrief.web import repo
from newsbrief.web.deps import Config, Conn, templates

log = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/healthz")
def healthz(conn: Conn) -> Response:
    """Readiness probe. Ready means the database answers, nothing more.

    Exempt from auth, because a probe cannot present credentials.
    """
    try:
        conn.execute("SELECT 1 FROM schema_version LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return JSONResponse({"status": "unready", "detail": str(exc)}, status_code=503)
    return JSONResponse({"status": "ok"})


@router.get("/health", response_class=HTMLResponse)
def source_health(request: Request, conn: Conn, settings: Config) -> Response:
    rows = repo.source_health(conn)
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "rows": rows,
            "timezone": settings.render_timezone,
            "max_failures": repo.MAX_CONSECUTIVE_FAILURES,
            "auto_disabled_count": sum(1 for row in rows if row.auto_disabled),
        },
    )


@router.post("/sources/{source_id}/revive")
def revive_source(conn: Conn, source_id: int) -> Response:
    """Re-enable an auto disabled source and clear its failure count."""
    if repo.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="no such source")
    repo.set_source_enabled(conn, source_id, True)
    log.info("source revived", source_id=source_id)
    return RedirectResponse("/health", status_code=303)
