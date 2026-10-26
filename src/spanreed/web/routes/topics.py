"""The topics list and the topic editor.

The `relevance_hint` field is the most important input in the application, so
the editor gives it room, explains what it does, and shows a worked example.
Everything else on this screen is a number with a sensible default.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from spanreed.sources import CONFIG_MODELS
from spanreed.web import repo
from spanreed.web.deps import Config, Conn, templates
from spanreed.web.forms import editable_fields

log = structlog.get_logger(__name__)

router = APIRouter()

KINDS = ("team", "tech")

HINT_EXAMPLE = (
    "Transfers, injuries, match results, and manager news for the first team. "
    "Not academy results, not ticketing announcements, and nothing about the "
    "unrelated real estate company with a similar name."
)


def _validated(kind: str, max_items: int, priority: int) -> tuple[str, int, int]:
    """Clamp the numbers and reject an unknown kind.

    The form uses a select and a number input, so bad values only arrive from a
    hand made request. Clamping beats a stack trace either way.
    """
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"unknown topic kind: {kind!r}")
    return kind, max(1, min(15, max_items)), max(1, min(100, priority))


@router.get("/", response_class=HTMLResponse)
def list_topics(request: Request, conn: Conn, settings: Config) -> Response:
    summaries = repo.topic_summaries(conn, datetime.now(UTC))
    return templates.TemplateResponse(
        request,
        "topics/list.html",
        {
            "summaries": summaries,
            "activity_days": repo.ACTIVITY_WINDOW_DAYS,
            "timezone": settings.render_timezone,
        },
    )


@router.get("/topics/new", response_class=HTMLResponse)
def new_topic(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "topics/edit.html",
        {
            "topic": None,
            "sources": [],
            "kinds": KINDS,
            "hint_example": HINT_EXAMPLE,
            "source_types": sorted(CONFIG_MODELS),
            "fields_by_type": {name: editable_fields(name) for name in CONFIG_MODELS},
        },
    )


@router.get("/topics/{topic_id}", response_class=HTMLResponse)
def edit_topic(request: Request, topic_id: int, conn: Conn, settings: Config) -> Response:
    topic = repo.get_topic(conn, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="no such topic")
    return templates.TemplateResponse(
        request,
        "topics/edit.html",
        {
            "topic": topic,
            "sources": repo.sources_for_topic(conn, topic_id),
            "kinds": KINDS,
            "hint_example": HINT_EXAMPLE,
            "source_types": sorted(CONFIG_MODELS),
            "fields_by_type": {name: editable_fields(name) for name in CONFIG_MODELS},
            "timezone": settings.render_timezone,
        },
    )


@router.post("/topics")
def create_topic(
    conn: Conn,
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    relevance_hint: Annotated[str, Form()],
    max_items: Annotated[int, Form()] = 3,
    priority: Annotated[int, Form()] = 50,
) -> Response:
    kind, max_items, priority = _validated(kind, max_items, priority)
    try:
        topic_id = repo.create_topic(
            conn,
            name=name.strip(),
            kind=kind,
            relevance_hint=relevance_hint.strip(),
            max_items=max_items,
            priority=priority,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"a topic named {name!r} already exists"
        ) from exc
    log.info("topic created", topic_id=topic_id, name=name)
    return RedirectResponse(f"/topics/{topic_id}", status_code=303)


@router.post("/topics/{topic_id}")
def save_topic(
    conn: Conn,
    topic_id: int,
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    relevance_hint: Annotated[str, Form()],
    max_items: Annotated[int, Form()] = 3,
    priority: Annotated[int, Form()] = 50,
) -> Response:
    if repo.get_topic(conn, topic_id) is None:
        raise HTTPException(status_code=404, detail="no such topic")
    kind, max_items, priority = _validated(kind, max_items, priority)
    try:
        repo.update_topic(
            conn,
            topic_id,
            name=name.strip(),
            kind=kind,
            relevance_hint=relevance_hint.strip(),
            max_items=max_items,
            priority=priority,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"a topic named {name!r} already exists"
        ) from exc
    return RedirectResponse(f"/topics/{topic_id}", status_code=303)


@router.post("/topics/{topic_id}/enabled", response_class=HTMLResponse)
def toggle_topic(
    request: Request,
    conn: Conn,
    topic_id: int,
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """Inline toggle on the topics list. Answers with the row it just changed."""
    if repo.get_topic(conn, topic_id) is None:
        raise HTTPException(status_code=404, detail="no such topic")
    repo.set_topic_enabled(conn, topic_id, enabled)

    summaries = repo.topic_summaries(conn, datetime.now(UTC))
    summary = next((s for s in summaries if s.topic.id == topic_id), None)
    if summary is None:
        raise HTTPException(status_code=404, detail="no such topic")
    return templates.TemplateResponse(request, "topics/_row.html", {"summary": summary})


@router.post("/topics/{topic_id}/delete")
def remove_topic(conn: Conn, topic_id: int) -> Response:
    """Deletes the topic's sources and items too, by way of ON DELETE CASCADE."""
    repo.delete_topic(conn, topic_id)
    log.info("topic deleted", topic_id=topic_id)
    return RedirectResponse("/", status_code=303)
