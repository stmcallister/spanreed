"""Persist the brief. This happens before any other delivery, always.

Email can fail, a feed reader can be down, the network can be out. None of that
is allowed to lose a brief that has already been paid for in tokens, so the
archive write is the first thing that happens once the markdown exists.

Re-running the composer for a date replaces that date's brief rather than
failing on the unique constraint, which is what makes the "generate now" button
in the UI safe to press twice.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import structlog
from markdown_it import MarkdownIt

from newsbrief import db
from newsbrief.compose.filter import Scored

log = structlog.get_logger(__name__)

# Raw HTML off. The markdown comes from a model working over feed content,
# and the archive page renders the result with |safe.
_MD = MarkdownIt("commonmark", {"html": False})

_UPSERT_BRIEF = """
INSERT INTO briefs (
    brief_date, generated_at, markdown, html, item_count,
    model, input_tokens, output_tokens, degraded, notes
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (brief_date) DO UPDATE SET
    generated_at  = excluded.generated_at,
    markdown      = excluded.markdown,
    html          = excluded.html,
    item_count    = excluded.item_count,
    model         = excluded.model,
    input_tokens  = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    degraded      = excluded.degraded,
    notes         = excluded.notes
"""

_SELECT_BRIEF_ID = "SELECT id FROM briefs WHERE brief_date = ?"

_DELETE_BRIEF_ITEMS = "DELETE FROM brief_items WHERE brief_id = ?"

_INSERT_BRIEF_ITEM = """
INSERT INTO brief_items (brief_id, item_id, rank, blurb) VALUES (?, ?, ?, ?)
"""


def render_html(markdown: str) -> str:
    """Render the brief to HTML for the archive page and for email."""
    return str(_MD.render(markdown))


def save(
    conn: sqlite3.Connection,
    *,
    brief_date: str,
    generated_at: str,
    markdown: str,
    selected: Sequence[Scored],
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    degraded: bool,
    notes: str | None,
) -> int:
    """Write the brief and its items. Returns the brief id.

    One short transaction, as every write in this application is. The HTML is
    rendered before the transaction opens so that no parsing happens while the
    write lock is held.
    """
    html = render_html(markdown)

    with db.write_transaction(conn):
        conn.execute(
            _UPSERT_BRIEF,
            (
                brief_date,
                generated_at,
                markdown,
                html,
                len(selected),
                model,
                input_tokens,
                output_tokens,
                int(degraded),
                notes,
            ),
        )
        row = conn.execute(_SELECT_BRIEF_ID, (brief_date,)).fetchone()
        brief_id = int(row["id"])

        # The item set is rewritten wholesale, so a regenerated brief does not
        # keep yesterday's choices alongside today's.
        conn.execute(_DELETE_BRIEF_ITEMS, (brief_id,))
        conn.executemany(
            _INSERT_BRIEF_ITEM,
            [(brief_id, entry.item.id, rank, None) for rank, entry in enumerate(selected, start=1)],
        )

    log.info(
        "brief archived",
        brief_id=brief_id,
        brief_date=brief_date,
        item_count=len(selected),
        degraded=degraded,
    )
    return brief_id
