"""The source editor and the test-source feature.

Test source is the feature that makes this tool usable. Refining a Google News
query interactively until it stops returning real estate listings takes
seconds. Waiting for tomorrow's brief to find out takes a day.

The test fetch never writes to `items`. It goes through the same adapter the
collector uses, so what it shows is exactly what would be stored, but nothing
it fetches is kept.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from newsbrief.sources import CONFIG_MODELS, build_client, discover, get_adapter
from newsbrief.sources.base import NotModifiedError
from newsbrief.sources.google_news import GoogleNewsConfig, build_url
from newsbrief.web import forms, repo
from newsbrief.web.deps import Config, Conn, templates

log = structlog.get_logger(__name__)

router = APIRouter()

TEST_PREVIEW_ITEMS = 5
"""Enough to see whether a query is returning the right kind of thing."""

TEST_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""The test fetch does no date filtering at all.

The collector only wants what is new. This feature answers a different
question: what does this source return? A slow moving feed filtered to the
last day looks broken when it is merely quiet, and that is exactly the
confusion the feature exists to prevent.
"""


# Literal paths are declared before the parameterised ones. Starlette matches
# in declaration order, so `/sources/{source_id}` registered first would swallow
# `/sources/test` and try to read "test" as an id.


@router.get("/sources/fields", response_class=HTMLResponse)
def source_fields(
    request: Request, source_type: Annotated[str, Query(alias="type")] = "rss"
) -> Response:
    """The inputs for one source type, swapped in when the type select changes."""
    if source_type not in CONFIG_MODELS:
        raise HTTPException(status_code=404, detail=f"unknown source type: {source_type!r}")
    return templates.TemplateResponse(
        request,
        "sources/_fields.html",
        {
            "source_type": source_type,
            "fields": forms.editable_fields(source_type),
            "values": {},
        },
    )


@router.get("/sources/google-news-url", response_class=HTMLResponse)
def google_news_url(request: Request, query: str = "", window: str = "1d") -> Response:
    """The constructed URL, rendered live as the query is typed.

    Seeing the quoting and the negative terms before saving is the difference
    between a query that works and a query that looked like it would.
    """
    try:
        url = build_url(GoogleNewsConfig(query=query, window=window))
        error = None
    except ValidationError as exc:
        url, error = None, "; ".join(item["msg"] for item in exc.errors())
    return templates.TemplateResponse(
        request, "sources/_google_url.html", {"url": url, "error": error}
    )


@router.post("/sources/test", response_class=HTMLResponse)
async def test_source(request: Request, settings: Config) -> Response:
    """Fetch a source right now and show what came back. Writes nothing.

    Works on an unsaved source, which is the point: the query gets refined
    before anything is stored.
    """
    form = await request.form()
    source_type = str(form.get("type") or "")
    config_dict = forms.config_from_form(source_type, form)
    config, error = forms.validate(source_type, config_dict)

    context: dict[str, object] = {"source_type": source_type, "items": [], "error": error}
    if config is None:
        return templates.TemplateResponse(request, "sources/_test.html", context)

    try:
        with build_client(settings) as client:
            adapter = get_adapter(source_type, client)
            if adapter is None:
                context["error"] = f"no adapter is registered for {source_type!r}"
                return templates.TemplateResponse(request, "sources/_test.html", context)
            items = adapter.fetch(config, TEST_EPOCH)
    except NotModifiedError:
        context["error"] = "the server answered 304, so there is nothing new to show"
        return templates.TemplateResponse(request, "sources/_test.html", context)
    except Exception as exc:
        context["error"] = f"{type(exc).__name__}: {exc}"
        log.info("source test failed", source_type=source_type, exc_info=True)
        return templates.TemplateResponse(request, "sources/_test.html", context)

    context["items"] = items[:TEST_PREVIEW_ITEMS]
    context["total"] = len(items)
    return templates.TemplateResponse(request, "sources/_test.html", context)


@router.get("/topics/{topic_id}/discover", response_class=HTMLResponse)
def discover_feeds(
    request: Request, topic_id: int, conn: Conn, settings: Config, url: str = ""
) -> Response:
    """Find the feeds on a site somebody pasted.

    Answers with every feed it could confirm by fetching it, each one ready to
    be added with one more click. Nothing is written until that click.
    """
    if repo.get_topic(conn, topic_id) is None:
        raise HTTPException(status_code=404, detail="no such topic")

    candidates = []
    if url.strip():
        with build_client(settings) as client:
            candidates = discover(client, url)

    return templates.TemplateResponse(
        request,
        "sources/_discovery.html",
        {"candidates": candidates, "topic_id": topic_id, "searched": bool(url.strip())},
    )


@router.post("/topics/{topic_id}/sources")
async def create_source(request: Request, conn: Conn, topic_id: int) -> Response:
    if repo.get_topic(conn, topic_id) is None:
        raise HTTPException(status_code=404, detail="no such topic")

    form = await request.form()
    source_type = str(form.get("type") or "")
    model, error = forms.validate(source_type, forms.config_from_form(source_type, form))
    if model is None:
        raise HTTPException(status_code=400, detail=error)

    source_id = repo.create_source(conn, topic_id, source_type, forms.config_from_model(model))
    log.info("source created", source_id=source_id, source_type=source_type, topic_id=topic_id)
    return RedirectResponse(f"/topics/{topic_id}", status_code=303)


@router.post("/sources/{source_id}")
async def save_source(request: Request, conn: Conn, source_id: int) -> Response:
    source = repo.get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="no such source")

    form = await request.form()
    source_type = str(form.get("type") or source.type)
    model, error = forms.validate(source_type, forms.config_from_form(source_type, form))
    if model is None:
        raise HTTPException(status_code=400, detail=error)

    merged = forms.merge_bookkeeping(source.config, forms.config_from_model(model))
    repo.update_source(conn, source_id, source_type, merged)
    return RedirectResponse(f"/topics/{source.topic_id}", status_code=303)


@router.post("/sources/{source_id}/enabled")
def toggle_source(conn: Conn, source_id: int, enabled: Annotated[bool, Form()] = False) -> Response:
    source = repo.get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="no such source")
    repo.set_source_enabled(conn, source_id, enabled)
    return RedirectResponse(f"/topics/{source.topic_id}", status_code=303)


@router.post("/sources/{source_id}/delete")
def remove_source(conn: Conn, source_id: int) -> Response:
    source = repo.get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="no such source")
    repo.delete_source(conn, source_id)
    log.info("source deleted", source_id=source_id)
    return RedirectResponse(f"/topics/{source.topic_id}", status_code=303)
