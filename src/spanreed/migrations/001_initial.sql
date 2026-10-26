CREATE TABLE topics (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    slug            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('team','tech')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    relevance_hint  TEXT NOT NULL,
    max_items       INTEGER NOT NULL DEFAULT 3,
    priority        INTEGER NOT NULL DEFAULT 50,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sources (
    id                   INTEGER PRIMARY KEY,
    topic_id             INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    type                 TEXT NOT NULL,
    config               TEXT NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    last_fetch_at        TEXT,
    last_status          TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    canonical_url TEXT,
    title         TEXT NOT NULL,
    title_norm    TEXT NOT NULL,
    summary_raw   TEXT,
    author        TEXT,
    source_name   TEXT,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    content_hash  TEXT NOT NULL,
    cluster_id    INTEGER,
    UNIQUE (topic_id, content_hash)
);

CREATE INDEX idx_items_topic_published ON items(topic_id, published_at DESC);

CREATE INDEX idx_items_fetched ON items(fetched_at DESC);

CREATE TABLE briefs (
    id            INTEGER PRIMARY KEY,
    brief_date    TEXT NOT NULL UNIQUE,
    generated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    markdown      TEXT NOT NULL,
    html          TEXT NOT NULL,
    item_count    INTEGER NOT NULL,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    degraded      INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE TABLE brief_items (
    brief_id INTEGER NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    rank     INTEGER NOT NULL,
    blurb    TEXT,
    PRIMARY KEY (brief_id, item_id)
);

CREATE TABLE fetch_log (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    run_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status     TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);

CREATE INDEX idx_fetch_log_source ON fetch_log(source_id, run_at DESC);
