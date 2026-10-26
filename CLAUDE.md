# spanreed

Personal daily news brief. Gathers items about a configured set of topics (sports teams and tech subjects), filters them for relevance, and produces one short brief each morning. A small web UI manages the topics and sources so adding a new interest does not require a code change or a redeploy.

This file is the contract for how the project is built. Follow it. If something here is wrong or you have a better approach, say so and ask before deviating.

---

## Hard constraints

These are decided. Do not relitigate them in code.

1. **Deployment target is k3s.** Two CronJobs plus one Deployment. No workflow engine, no message queue, no Temporal, no Celery, no Redis. If a problem seems to need an orchestrator, it needs a simpler design instead.
2. **GitOps via ArgoCD.** Everything deploys from manifests in this repo. No `kubectl apply` as part of any documented workflow.
3. **The brief is short.** Hard cap of 15 items. This is enforced in code, not left to a prompt. A brief that grows unbounded is a brief that stops being read.
4. **Gathering is deterministic. Only judgment uses a model.** Fetching, parsing, deduping, and storing are plain code with no LLM involvement. The model filters for relevance and writes prose. Nothing else.
5. **A dead source degrades the brief. It never empties it.** Every source failure is caught, recorded, and reported. One broken feed must not prevent the brief from being generated.
6. **Single-user.** No accounts, no roles, no multi-tenancy, no password reset flow.

---

## Architecture

Three workloads, one shared SQLite database, one container image.

```
                    ┌──────────────────────┐
 CronJob            │ collector            │
 every 3 hours      │ fetch all sources    │──┐
                    │ normalize + store    │  │
                    └──────────────────────┘  │
                                              ▼
                    ┌──────────────────────┐ ┌─────────────┐
 Deployment         │ web                  │ │ SQLite      │
 always on          │ manage topics        │◄┤ on a PVC    │
                    │ test sources         │ └─────────────┘
                    │ read brief archive   │  ▲
                    └──────────────────────┘  │
                                              │
                    ┌──────────────────────┐  │
 CronJob            │ composer             │──┘
 06:00 America/     │ cluster, filter,     │
 Los_Angeles        │ write, deliver       │
                    └──────────────────────┘
```

The collector runs on a schedule independent of the brief so that news arriving at 2pm is captured at 3pm rather than waiting for a single overnight fetch that might fail. The composer only reads. It never fetches feeds. This separation is the entire reliability story, so keep it clean.

---

## Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | One language across all three entrypoints |
| HTTP | `httpx` | Timeouts and retries that actually work |
| Feed parsing | `feedparser` | Handles the malformed RSS that is everywhere |
| HTML parsing | `selectolax` | Fast, and enough for feed discovery and article extraction |
| Fuzzy matching | `rapidfuzz` | Title-similarity dedupe |
| Web framework | `FastAPI` | Serves both the UI and a small JSON API |
| Templating | `Jinja2` + `HTMX` | No npm, no build step, no bundler in the image |
| Styling | Pico.css, vendored | One file, looks fine, zero tooling |
| Markdown rendering | `markdown-it-py` | The `briefs.html` column needs a renderer and the stdlib has none |
| DB access | `sqlite3` stdlib | The schema is small and SQL is clearer here than an ORM |
| Migrations | Numbered `.sql` files | Applied in order at startup, tracked in a `schema_version` table |
| LLM | `anthropic` SDK | See the LLM section |
| Config | `pydantic-settings` | Env vars with validation |
| Tests | `pytest` + `respx` | Recorded HTTP fixtures, no network in tests |

The HTMX choice is deliberate. This UI is a handful of forms. A React build step would be the single most annoying part of the deployment pipeline and would buy nothing.

---

## Repository layout

```
.
├── CLAUDE.md
├── pyproject.toml
├── Dockerfile
├── src/spanreed/
│   ├── __main__.py           # entrypoint dispatch: collect | compose | web
│   ├── config.py
│   ├── db.py                 # connection, pragmas, migration runner
│   ├── migrations/
│   │   └── 001_initial.sql
│   ├── models.py             # dataclasses, not an ORM
│   ├── sources/
│   │   ├── base.py           # SourceAdapter protocol
│   │   ├── rss.py
│   │   ├── google_news.py
│   │   ├── espn.py
│   │   ├── reddit.py
│   │   ├── bluesky.py
│   │   ├── rsshub.py
│   │   └── discovery.py      # find feeds on an arbitrary site
│   ├── collect.py
│   ├── compose/
│   │   ├── cluster.py        # lexical, no LLM
│   │   ├── filter.py         # LLM relevance pass
│   │   ├── deepread.py       # bounded full-text pass
│   │   ├── write.py          # LLM prose pass
│   │   └── pipeline.py
│   ├── deliver/
│   │   ├── email.py
│   │   └── archive.py
│   ├── llm.py                # single wrapper around the Anthropic SDK
│   └── web/
│       ├── app.py
│       ├── routes/
│       ├── templates/
│       └── static/
├── deploy/
│   ├── base/
│   └── overlays/kholinar/
├── seed/topics.yaml          # bootstrap only, see below
└── tests/
    └── fixtures/             # recorded feed and API responses
```

---

## Data model

SQLite. Open every connection with these pragmas, without exception:

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
```

WAL is what makes three pods sharing one file safe. All three workloads mount the same `ReadWriteOnce` PVC, which requires that they schedule onto the same node. Set a `nodeSelector` on all three to pin them. Keep every write transaction short. Never hold a transaction open across an HTTP call or an LLM call.

```sql
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
    config               TEXT NOT NULL,          -- JSON, shape depends on type
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
    id           INTEGER PRIMARY KEY,
    brief_date   TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    markdown     TEXT NOT NULL,
    html         TEXT NOT NULL,
    item_count   INTEGER NOT NULL,
    model        TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    degraded     INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
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
```

**The database is the source of truth for topics.** `seed/topics.yaml` exists only to bootstrap an empty database on first run. Once the UI writes a topic, the YAML is stale and irrelevant. Provide `spanreed export-topics` that dumps the current topics and sources back to YAML so that configuration is recoverable if the PVC is lost. Do not attempt to write back to git from the running application.

Retention: delete items older than 60 days at the end of each composer run. Keep briefs forever, they are small.

---

## Source adapters

Every adapter implements this protocol:

```python
class SourceAdapter(Protocol):
    type: ClassVar[str]
    ConfigModel: ClassVar[type[BaseModel]]

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]: ...
```

`RawItem` carries url, title, summary, author, source_name, published_at. Normalization into `items` happens once, centrally, in `collect.py`. Adapters do not touch the database.

Every adapter must tolerate a malformed or restructured response by returning fewer items or an empty list. An adapter raising an unhandled exception is a bug.

### rss
Generic feed URL. Config: `{"url": "..."}`. Send `If-Modified-Since` and `ETag` when the previous response supplied them, and store them in the source config.

### google_news
This is the workhorse for teams and topics that mainstream outlets ignore. Config: `{"query": "...", "window": "1d"}`.

```
https://news.google.com/rss/search?q={quote(query)}+when:{window}&hl=en-US&gl=US&ceid=US:en
```

Quoted phrases and negative terms both work in `query`, so `"Midlakes United" -realty` is a valid query. Item links are Google redirect URLs. Store the redirect URL as `url` and attempt a single HEAD resolution to populate `canonical_url`, but treat failure as normal and move on. Do not build a retry loop around link resolution.

### espn
Config: `{"sport": "soccer", "league": "usa.1", "team_id": "..."}`. Undocumented endpoints, no key required:

```
https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team_id}/schedule
https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
```

Parse defensively with `.get()` chains throughout. Assume the shape will change without warning. Emit completed results and upcoming fixtures as items with a synthetic title such as `Final: Team A 2, Team B 1`. Set `published_at` from the event date, not from fetch time.

### reddit
Config: `{"subreddit": "...", "listing": "top", "window": "day"}`, producing `https://reddit.com/r/{sub}/{listing}/.rss?t={window}`. **A descriptive User-Agent is mandatory** or requests will be blocked. Use `spanreed/1.0 (+https://github.com/stmcallister/spanreed)`. Rate limit to one request every two seconds across all Reddit sources.

### bluesky
Config: `{"handle": "..."}`, producing `https://bsky.app/profile/{handle}/rss`. Often the only machine-readable output a small club publishes.

### rsshub
Config: `{"route": "..."}`, resolved against `RSSHUB_BASE_URL`. Optional, skip the source cleanly if the base URL is unset.

### discovery
Not a fetch adapter. A helper the UI calls when the user pastes a site URL. It should:
1. Fetch the page and read `<link rel="alternate" type="application/rss+xml">` from the head. This is the correct method and usually succeeds.
2. Fall back to probing `/feed/`, `/rss`, `/feed.xml`, `/atom.xml`, `/index.xml`, `/blog/feed/`.
3. Return every candidate with its title and item count so the user can pick.

---

## Collector

```
for each enabled source:
    try:
        items = adapter.fetch(config, since=last_fetch_at or now-48h)
        normalize and insert, ignoring UNIQUE conflicts
        record success, reset consecutive_failures
    except Exception as e:
        log, record failure, increment consecutive_failures
        continue
```

Normalization:
- `title_norm`: lowercase, strip a trailing source attribution (` - The Verge`, ` | Ars Technica`), remove punctuation, collapse whitespace.
- `content_hash`: sha256 of `title_norm` plus the canonical or raw URL host. This makes the `UNIQUE (topic_id, content_hash)` constraint the primary defense against re-inserting the same item on every run.

Auto-disable a source after 20 consecutive failures and surface it prominently in the UI. Do not delete it.

The collector must exit 0 even when some sources fail. It exits non-zero only if it cannot open the database.

---

## Composer

Four stages. Each stage degrades rather than aborts.

### 1. Cluster (no LLM)
Pull items from the last 24 hours. Within each topic, group by `rapidfuzz.fuzz.token_set_ratio(title_norm_a, title_norm_b) >= 88`. Pick the cluster representative by source priority, preferring the original publisher over an aggregator. This is deterministic and cheap, so do not use a model for it.

**Key on normalized title, never on URL alone.** A syndicated wire story arrives under a dozen distinct URLs and URL-only dedupe lets all twelve through.

### 2. Relevance filter (LLM)
This is the load-bearing stage. Keyword search invites noise, so the filter is what makes the brief readable.

Batch up to 40 cluster representatives per call, grouped by topic so the topic's `relevance_hint` applies to the whole batch. Ask for structured output:

```json
[{"id": 123, "keep": true, "score": 0.86, "reason": "roster move"}]
```

Use `claude-haiku-4-5-20251001`. Classification is exactly what it is for and the cost is negligible.

Drop anything with `keep: false` or `score < 0.5`. Then rank by `score * (topic.priority / 50)` and take the top 15, respecting each topic's `max_items` so one busy topic cannot crowd out everything else.

### 3. Deep read (bounded)
For the top 3 items only, fetch the article and extract the main text with `selectolax`. Cap at 6 seconds and 40KB per fetch. On failure, fall back to the feed summary. This stage is why the top items read like something a person wrote rather than a rearranged headline.

### 4. Write (LLM)
One call with the selected items grouped by topic. Use `claude-sonnet-5`. Output markdown.

Style rules for the generated brief, and these are firm:
- **No em dashes and no double hyphens anywhere in the output.** Use commas, colons, parentheses, or separate sentences.
- One or two sentences per item. Three at most for a deep-read item.
- Lead with what happened, not with framing like "in a recent development".
- No invented facts. If the source material does not say it, it does not appear.
- Group by topic, with sports results first.

### Failure behavior
If the filter call fails after retries, fall back to top-N by source priority and recency, set `briefs.degraded = 1`, and note it in the brief. If the write call fails, emit a plain formatted list of the selected items. A degraded brief is always better than no brief.

Footer on every brief: a one-line list of any sources that failed in the last 24 hours. That is the whole observability story and it is sufficient.

---

## LLM usage

Wrap the SDK in `llm.py`. Nothing else in the codebase imports `anthropic` directly.

- Models: `claude-haiku-4-5-20251001` for filtering, `claude-sonnet-5` for writing. Both configurable via env.
- Use the SDK's structured output support for the filter stage. Do not parse prose with regex, and do not ask for JSON in a free-text response when structured output is available.
- Retry on 429 and 5xx with exponential backoff, three attempts maximum. Never retry a 400.
- Record `input_tokens` and `output_tokens` on the brief row. Expected cost is a few cents per day, and the token record is how you notice when that stops being true.
- Set `max_tokens` explicitly on every call.

---

## Delivery

1. **Archive**, always. Persist markdown and HTML to the `briefs` table and serve at `/briefs/{YYYY-MM-DD}`.
2. **Email**, optional. SMTP via env config. Skip cleanly when unconfigured.
3. **RSS feed** of briefs at `/feed.xml`.

Archive first, then email. A delivery failure must not lose the generated brief.

---

## Web UI

Server-rendered Jinja2 with HTMX for partial updates. Five screens.

**Topics list.** Every topic with kind, enabled state, source count, and item count over the last 7 days. Inline enable and disable toggles.

**Topic editor.** Name, kind, `relevance_hint`, `max_items`, `priority`. The `relevance_hint` field is the most important input in the whole application, so give it a large textarea, a clear label explaining that it tells the filter what counts as relevant, and a worked example in the placeholder.

**Source editor.** Add a source to a topic. Pick the type, then render fields specific to that type. For `google_news`, show the constructed URL live as the query is typed so that quoting and negative terms are visible before saving.

**Test source.** The feature that makes this tool usable. A button on any unsaved or saved source that fetches immediately and renders the last 5 items in place. Refining a Google News query interactively until it stops returning real estate listings is far faster than waiting for tomorrow's brief to find out. This must not write to `items`.

**Source health.** Sources sorted by `consecutive_failures` descending, showing last status, last error, and last successful fetch. Auto-disabled sources pinned to the top.

**Brief archive.** List of dates, rendered brief on click, plus a "generate now" button that runs the composer against today's items for testing.

Auth: HTTP basic against a single credential from env, enforced by middleware. It sits behind Tailscale on the homelab, so this is proportionate. Do not build a login page.

---

## Deployment

Kustomize with `deploy/base` and `deploy/overlays/kholinar`, structured so ArgoCD points at the overlay.

One image, three entrypoints:
```yaml
command: ["python", "-m", "spanreed"]
args: ["collect"]     # or "compose" or "web"
```

**collector CronJob**
```yaml
schedule: "0 */3 * * *"
concurrencyPolicy: Forbid
successfulJobsHistoryLimit: 3
failedJobsHistoryLimit: 3
backoffLimit: 2
activeDeadlineSeconds: 900
```

**composer CronJob**
```yaml
schedule: "0 6 * * *"
timeZone: "America/Los_Angeles"
concurrencyPolicy: Forbid
backoffLimit: 1
activeDeadlineSeconds: 600
```

`activeDeadlineSeconds` matters more than it looks. Without it a hung feed leaves a pod running until the next schedule, and with `Forbid` that silently skips runs.

Also required: namespace, PVC using `local-path`, Deployment for web with a readiness probe on `/healthz`, Service, Ingress, ConfigMap for non-secret config, and resource requests and limits on all three.

**Secrets are never committed in plaintext.** Reference an externally managed Secret by name and document that `ANTHROPIC_API_KEY`, the basic auth credential, and any SMTP settings are supplied through whichever mechanism is already in use on the cluster. Do not add a plaintext Secret manifest to the repo, even as an example.

Migrations run at container start in all three entrypoints, guarded by the `schema_version` table so concurrent starts are safe.

---

## Conventions

- Type hints everywhere. `mypy --strict` passes.
- `ruff` for lint and format, configured in `pyproject.toml`.
- Structured logging via `structlog`, JSON to stdout. Every log line from the collector carries `source_id` and `source_type`.
- Dataclasses for domain objects, Pydantic only for config validation and adapter config shapes.
- No bare `except:`. Catch `Exception` where a source or stage must degrade, and log with the traceback.
- All timestamps stored as ISO 8601 UTC strings. Convert to America/Los_Angeles only at render time.
- No em dashes or double hyphens in code comments, docstrings, log messages, UI copy, or generated output.

---

## Testing

- Every adapter has tests against recorded fixtures in `tests/fixtures/`. No network in the test suite, enforced by an autouse guard in `conftest.py` that fails any test attempting a non-loopback connection or DNS lookup.
- Each adapter has an explicit test where the fixture is truncated or restructured, asserting that it returns fewer items rather than raising. This matters most for ESPN.
- Dedupe tests covering the syndicated-story case: same story, six sources, six URLs, one surviving item.
- Composer pipeline tests with the LLM wrapper stubbed, covering both the happy path and the degraded path.
- A smoke test that runs migrations against a temp database and asserts the schema version.

---

## Build order

Do not build this all at once. Stop after each phase and confirm.

1. Project skeleton, config, database layer, migrations, `__main__` dispatch.
2. `rss` and `google_news` adapters plus the collector. Verify real items land in SQLite.
3. Clustering and dedupe with no LLM. Verify duplicates collapse correctly.
4. LLM filter and write stages. Produce a real brief to stdout.
5. Web UI: topics, sources, and the test-source feature.
6. Kubernetes manifests and the Dockerfile.
7. Remaining adapters: `espn`, `reddit`, `bluesky`, `rsshub`, `discovery`.
8. Email delivery and the RSS feed.

---

## Gotchas

- Reddit blocks requests without a descriptive User-Agent. This will be the first thing that breaks.
- Google News item links are redirects. Do not assume `url` is the publisher's URL.
- ESPN endpoints are undocumented and will change shape eventually. Defensive parsing is not optional.
- SQLite across three pods needs WAL, short transactions, and all three pods on the same node.
- `published_at` is frequently missing or wrong in feeds. Fall back to `fetched_at` and never let a missing date crash normalization.
- A topic named after a common word or place will pull in unrelated news. That is what `relevance_hint` and negative query terms are for, and it is why the test-source feature exists.
