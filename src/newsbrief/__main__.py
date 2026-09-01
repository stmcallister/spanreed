"""Entrypoint dispatch.

One image, three commands. Every command runs migrations first, which is why
the containers can all start at once without a separate migration job.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

import structlog

from newsbrief import db
from newsbrief.config import Settings, get_settings

COMMANDS = ("collect", "compose", "web")

USAGE = f"""usage: python -m newsbrief <command>

commands:
  {"  ".join(COMMANDS)}

  collect   fetch every enabled source and store new items
  compose   cluster, filter, write, and deliver today's brief
  web       serve the management UI and JSON API
"""


def configure_logging(level: str) -> None:
    """Structured JSON logging to stdout, which is all the cluster needs."""
    level_number = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_number),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def run_migrations(settings: Settings) -> int:
    """Bring the schema up to date. Fails loudly, because nothing else can run."""
    conn = db.connect(settings.db_path)
    try:
        return db.migrate(conn)
    finally:
        conn.close()


def dispatch(command: str, settings: Settings) -> int:
    """Hand off to the requested workload.

    Imports are deferred so that the collector does not pay for FastAPI and the
    web pod does not pay for the composer.
    """
    if command == "collect":
        from newsbrief.collect import run as run_collect

        return run_collect(settings)

    if command == "compose":
        from newsbrief.compose.pipeline import run as run_compose

        return run_compose(settings)

    if command == "web":
        from newsbrief.web.app import run as run_web

        return run_web(settings)

    raise AssertionError(f"unhandled command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("help", "-h", "--help"):
        sys.stdout.write(USAGE)
        return 0 if args else 2

    command = args[0]
    if command not in COMMANDS:
        sys.stderr.write(f"unknown command: {command}\n\n{USAGE}")
        return 2

    settings = get_settings()
    configure_logging(settings.log_level)
    log = structlog.get_logger("newsbrief")

    try:
        schema_version = run_migrations(settings)
    except Exception:
        log.exception("could not open the database", db_path=str(settings.db_path))
        return 1

    log.info(
        "starting",
        command=command,
        db_path=str(settings.db_path),
        schema_version=schema_version,
    )

    try:
        return dispatch(command, settings)
    except NotImplementedError as exc:
        log.error("command not implemented yet", command=command, detail=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
