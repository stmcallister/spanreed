"""Web workload: topics, sources, source testing, and the brief archive.

Server rendered Jinja2 with HTMX for the partial updates. No npm, no bundler,
no build step in the image. This UI is a handful of forms and a table, and a
frontend toolchain would be the most annoying part of the deployment pipeline
while buying nothing.
"""

from __future__ import annotations

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from spanreed.config import Settings
from spanreed.web.auth import BasicAuthMiddleware
from spanreed.web.deps import STATIC_DIR, local_time, relative_time, templates
from spanreed.web.routes import briefs, health, sources, topics

log = structlog.get_logger(__name__)


def create_app(settings: Settings) -> FastAPI:
    """Build the application. Separate from `run` so tests can drive it directly."""
    app = FastAPI(title="spanreed", docs_url=None, redoc_url=None)
    app.state.settings = settings

    app.add_middleware(BasicAuthMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates.env.filters["local_time"] = local_time
    templates.env.filters["relative_time"] = relative_time

    # Health first, so /healthz is registered before anything can shadow it.
    app.include_router(health.router)
    app.include_router(topics.router)
    app.include_router(sources.router)
    app.include_router(briefs.router)

    @app.exception_handler(StarletteHTTPException)
    async def render_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Errors are pages, not JSON. This UI is read by a person."""
        if request.url.path.startswith("/healthz"):
            raise exc
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    return app


def run(settings: Settings) -> int:
    """Serve the management UI. Returns the process exit code."""
    log.info("web starting", host=settings.web_host, port=settings.web_port)
    uvicorn.run(
        create_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,
        access_log=False,
    )
    return 0
