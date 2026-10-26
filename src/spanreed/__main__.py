"""Entrypoint dispatch.

One image, three commands. Every command runs migrations first, which is why
the containers can all start at once without a separate migration job.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import TextIO, cast

import structlog

from spanreed import db
from spanreed.config import Settings, get_settings

COMMANDS = ("collect", "compose", "web", "export-topics")

READ_ONLY_COMMANDS = frozenset({"export-topics"})
"""Commands that must not bootstrap, and must not log onto stdout."""

USAGE = f"""usage: python -m spanreed <command>

commands:
  {"  ".join(COMMANDS)}

  collect        fetch every enabled source and store new items
  compose        cluster, filter, write, and deliver today's brief
  web            serve the management UI and JSON API
  export-topics  dump topics and sources to stdout as YAML

An empty database is seeded from SEED_PATH on first start. Once a topic
exists the seed file is never consulted again.
"""


class _Stderr:
    """A stand in for stderr that resolves the stream when it writes.

    structlog holds whatever file it is configured with for the life of the
    process. Handing it `sys.stderr` directly captures that object, and
    anything that later replaces the stream, a supervisor, a wrapper, or
    pytest's capture, leaves the logger writing to a handle that has since been
    closed. Looking it up per write costs nothing and cannot go stale.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: str, *, to_stderr: bool = False) -> None:
    """Structured JSON logging to stdout, which is all the cluster needs.

    `export-topics` sends it to stderr instead, because its stdout is the YAML
    and a log line in the middle of that would corrupt the file somebody is
    redirecting it into.
    """
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
        # structlog annotates this as TextIO but only ever calls write and
        # flush on it, which is all the proxy above provides.
        logger_factory=structlog.PrintLoggerFactory(
            file=cast(TextIO, _Stderr()) if to_stderr else None
        ),
        cache_logger_on_first_use=True,
    )


def run_migrations(settings: Settings) -> int:
    """Bring the schema up to date. Fails loudly, because nothing else can run."""
    conn = db.connect(settings.db_path)
    try:
        return db.migrate(conn)
    finally:
        conn.close()


def bootstrap_topics(settings: Settings) -> None:
    """Seed an empty database. Never fails the start.

    Configuration that could not be read is worth a loud log line and is not
    worth refusing to run over, because the UI can still be used to enter it.
    """
    from spanreed import seed

    conn = db.connect(settings.db_path)
    try:
        seed.bootstrap(conn, settings.seed_path)
    except Exception:
        structlog.get_logger("spanreed").exception(
            "could not bootstrap from the seed file", seed_path=str(settings.seed_path)
        )
    finally:
        conn.close()


def export_topics(settings: Settings) -> int:
    """Write the live configuration to stdout as YAML."""
    from spanreed import seed

    conn = db.connect(settings.db_path)
    try:
        sys.stdout.write(seed.export(conn))
    finally:
        conn.close()
    return 0


def dispatch(command: str, settings: Settings) -> int:
    """Hand off to the requested workload.

    Imports are deferred so that the collector does not pay for FastAPI and the
    web pod does not pay for the composer.
    """
    if command == "collect":
        from spanreed.collect import run as run_collect

        return run_collect(settings)

    if command == "compose":
        from spanreed.compose.pipeline import run as run_compose

        return run_compose(settings)

    if command == "web":
        from spanreed.web.app import run as run_web

        return run_web(settings)

    if command == "export-topics":
        return export_topics(settings)

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
    read_only = command in READ_ONLY_COMMANDS
    configure_logging(settings.log_level, to_stderr=read_only)
    log = structlog.get_logger("spanreed")

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

    if not read_only:
        bootstrap_topics(settings)

    try:
        return dispatch(command, settings)
    except NotImplementedError as exc:
        log.error("command not implemented yet", command=command, detail=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
