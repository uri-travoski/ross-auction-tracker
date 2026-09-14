# AGENTS.md — Architecture and Extension Guide

This document is for AI agents (and humans) who need to understand, extend, or debug the Ross Auction Tracker. It documents the architecture, conventions, extension points, and common tasks.

## Architecture overview

The system is a single Python application (`auction_tracker`) that runs unattended inside a Docker container. It has no external dependencies beyond SQLite (bundled with Python), optional SMTP, and optional AI provider APIs.

### Data flow

```
Ross's Auctions (HTTP/Playwright)
  -> fetch/ (retry, auto-fallback)
  -> scrape/ (parse index, detail, feeds; classify IT)
  -> changes.py (diff against last scan)
  -> store.py -> db.py (SQLite, WAL)
  -> images.py (download thumbnails + originals)
  -> notify/ (SMTP or log-only)
  -> ai/ (classification, extraction, estimates, Q&A — fail-open)
  -> web/app.py (Flask UI on :8080)
  -> scheduler.py (APScheduler in-process: discovery, scans, reminders)
```

### Configuration layering

Configuration is resolved in `config.py` with later sources winning:

1. `DEFAULTS` dict in `config.py`
2. `config.yaml` (non-secret behavioural settings)
3. `.env` file (secrets)
4. Real environment variables
5. `AT__SECTION__KEY` nested overrides (e.g. `AT__WEB__PAGE_SIZE=250`)

Secrets (SMTP passwords, AI keys) must come from environment variables, never from YAML.

### Key abstractions

- **`Store`** (`store.py`): The repository layer. All database access goes through `Store` methods. Application code never writes SQL directly. This makes the storage layer swappable and testable.
- **`Fetcher`** (`fetch/base.py`): Abstract fetcher with retry, politeness delay, and validator support. Concrete implementations: `HttpFetcher` (requests + urllib fallback), `PlaywrightFetcher` (Chromium). `auto` mode tries HTTP first and escalates to Playwright if the validator rejects the response.
- **`Classifier`** (`scrape/classify.py`): Two-stage IT filter — keyword pre-filter (fast, deterministic) then optional AI confirmation. Fail-open: if AI is unavailable, the keyword verdict stands.
- **`AIEngine`** (`ai/`): Multi-provider AI with per-task fallback, response cache (keyed by task + input hash), per-cycle call budget, and fail-open behavior. Tasks: classification, spec extraction, scan summaries, price estimates, free-form questions.
- **`Notifier`** (`notify/`): SMTP notifier with StartTLS/SSL/no-security modes, multiple recipients, separate reminder recipients, hourly rate limiting, reminder deduplication, and dry-run mode. `LogOnlyNotifier` is the fallback when SMTP is not configured.
- **`Pipeline`** (`pipeline.py`): Orchestrates discovery and change scans. Handles the initial-capture regression: the first scan of an auction generates `NEW_LOT` changes for every lot, but the significant-changes notification is suppressed because the new-auction notification already covers the baseline.
- **`Scheduler`** (`scheduler.py`): In-process APScheduler with four jobs: discovery (24h), change scan (6h), heartbeat (30min — final-stretch polling, reminders, finalization), and report generation.

### Selectors

All site-specific CSS selectors and regex patterns are centralized in `selectors.py`. This makes the scraper resilient to minor site changes — update one file.

### Database

SQLite with WAL mode for concurrent read/write. Schema is versioned with a `schema_version` table. Migrations are in `db.py` and run automatically on startup. Current schema version: 2.

Key tables: `auctions`, `lots`, `changes`, `images`, `ai_cache`, `notifications`, `reminders`, `cycles`, `comparable_lots`.

## How to change behaviour

### Change the scan schedule

In `config.yaml` under `schedule:` or via environment:

```bash
AT__SCHEDULE__CHANGE_EVERY_HOURS=3
AT__SCHEDULE__DISCOVERY_CRON_HOUR=6
AT__SCHEDULE__FINAL_STRETCH_MINUTES=120
```

### Add a new IT keyword

In `config.yaml` under `filter.it_keywords:` — add the keyword. The keyword matcher uses word boundaries (so `laptop` matches `Laptop` but not `Laptops`).

### Change the fetcher

```bash
AT__FETCH__CLIENT=playwright   # always use Playwright
AT__FETCH__CLIENT=http         # always use HTTP (requests)
AT__FETCH__CLIENT=auto         # HTTP first, Playwright fallback (default)
```

### Add an AI provider

In standalone Docker deployments, `config.yaml` is baked into the image with all default providers. Custom configuration can be placed in `./data/config.yaml` or supplied via environment variables.


In `config.yaml` under `ai.providers:`, add a new provider entry with:
- `name`: unique identifier
- `kind`: `"openai"` (for `/chat/completions`) or `"anthropic"` (for `/messages`)
- `base_url`: API base URL (e.g. `https://api.groq.com/openai/v1` or `http://host.docker.internal:8000/v1`)
- `model`: model identifier string
- `api_key_env`: name of the environment variable in `.env` holding its key (or set `require_api_key: false` for unauthenticated local models)
- `enabled`: `true`

Then add the provider's `name` to the preferred tasks under `ai.tasks` (`classify`, `extract_specs`, `summarize_scan`, `estimate_price`). The engine tries providers in order for each task and falls back to the next on failure.

### Change notification recipients

In `.env`:

```bash
SMTP_RECIPIENTS=alice@example.com,bob@example.com
SMTP_REMINDER_RECIPIENTS=carol@example.com   # only gets pre-close reminders
```

### Enable web UI authentication

In `.env`:

```bash
WEB_USERNAME=admin
WEB_PASSWORD=secret
```

`/healthz` is always accessible without auth (for Docker healthchecks).

## Testing

### Conventions

- Tests are in `tests/` and use `pytest`.
- Fixtures are captured HTML/JSON from the real site, stored in `tests/fixtures/`.
- Tests are network-free by default. Live tests are gated behind `AUCTION_TRACKER_LIVE=1`.
- `runtests.py` is the canonical entry point (used by Docker test profile and CI).
- `conftest.py` provides shared fixtures: `config`, `store`, `fake_fetcher`, fixture HTML/JSON loaders.

### Running tests

```bash
python runtests.py                          # full suite
python -m pytest tests/test_index_page.py   # single module
python -m pytest tests/ -k baseline         # pattern match
AUCTION_TRACKER_LIVE=1 python -m pytest -k live  # live tests
```

### Adding a test

1. If you need new fixture data, capture it from the real site and save it under `tests/fixtures/`.
2. Use the `fake_fetcher` fixture to seed canned responses.
3. Use the `store` fixture (in-memory SQLite) for database tests.
4. Use the `config` fixture for configuration.
5. Avoid network access unless the test is marked `live`.

## Docker

### Build

```bash
docker build -t ghcr.io/uri-travoski/ross-auction-tracker:latest .
```

### Run

```bash
docker compose up -d
```

### Verify

```bash
curl http://localhost:8080/healthz   # should return "ok"
docker compose logs -f auction-tracker
```

### Test inside the container

```bash
docker compose --profile test run --rm test
```

## Common tasks

### Add a new migration

1. Increment `SCHEMA_VERSION` in `db.py`.
2. Add a migration function in `db.py` under the migrations section.
3. Register it in the migrations list.
4. Add a test in `tests/test_db.py`.

### Add a new notification event

1. Add the event type to `models.py` constants.
2. Add a message builder in `notify/messages.py`.
3. Add a method to `notify/base.py` and implementations in `notify/email_smtp.py` and `notify/log_only.py`.
4. Call it from `pipeline.py` at the appropriate point.
5. Add a test in `tests/test_notify.py`.

### Add a new web route

1. Add the route to `web/app.py`.
2. Add a template in `web/templates/`.
3. Add a test in `tests/test_web.py` using the Flask test client.

### Debug a failing scrape

1. Set `AT__LOGGING__LEVEL=DEBUG` to see detailed fetch/parse logs.
2. Set `AT__FETCH__CLIENT=playwright` to try the browser fetcher.
3. Check `data/logs/auction-tracker.log` for errors.
4. Use `python -m auction_tracker classify <url>` to see how a single auction is classified.
5. Compare the live page HTML against the fixtures in `tests/fixtures/`.

## Security notes

- Never commit `.env`, `data/`, or any file containing secrets.
- The GitHub token previously exposed in conversation must be rotated.
- Use fine-grained PATs with minimal scopes for GHCR push.
- SMTP passwords and AI API keys come from environment variables only.
