# Ross Auction Tracker

An unattended, Docker-deployable agent that monitors [Ross's Auctions](https://auctions.com.au) for IT-related online auctions, captures complete lot catalogues, tracks bid history and listing changes over time, downloads images, sends SMTP notifications with pre-close reminders, and exposes a clean, modern Slate & Indigo web UI for browsing current and past auctions, comparing lots, and asking an AI what to pay based on historical comparables.

## Quick start

```bash
# 1. Clone and configure
git clone https://github.com/uri-travoski/ross-auction-tracker.git
cd ross-auction-tracker
cp .env.example .env       # edit .env with your SMTP + AI keys

# 2. Pull and run the prebuilt image
docker compose up -d

# 3. Open the dashboard
open http://localhost:8080
```

If you prefer to build locally instead of pulling from GHCR:

```bash
docker build -t ghcr.io/uri-travoski/ross-auction-tracker:latest .
docker compose up -d
```

## What it does

- **Discovers** online auctions from the Ross's Auctions index every 24 hours.
- **Filters** for IT-related auctions using configurable keyword rules and optional AI classification.
- **Captures** auction metadata, complete lot catalogues (lot numbers, quantities, descriptions, images, bid URLs, times).
- **Tracks** bid changes, bid-count changes, reserve status, close-time extensions, description edits, image changes, and added/removed lots.
- **Downloads** thumbnails and original images to disk, deduplicated by URL and SHA-256.
- **Persists** everything in SQLite (auctions, lots, bids, changes, images, AI cache, notifications, cycles).
- **Notifies** via SMTP: new auctions, significant changes, pre-close reminders (3 hours and 30 minutes before closing), extensions, and finalization.
- **Estimates** a maximum price for a lot based on historical comparable sales using AI.
- **Serves** a Flask web UI with search, filters, comparisons, change history, stored images, and an AI question endpoint.
- **Runs** unattended with an in-process APScheduler (no cron required).

## Architecture

```
auction_tracker/
  cli.py            # CLI entry points (serve, discover, scan, status, ask, ...)
  config.py         # Layered config: defaults -> config.yaml -> .env -> env vars
  models.py         # Dataclasses and status/change/cycle constants
  db.py             # SQLite, WAL, migrations, schema versioning
  store.py          # Repository layer (application code never writes SQL)
  selectors.py      # Centralized site selectors and regex patterns
  fetch/            # HTTP + Playwright fetchers with retry and auto-fallback
  scrape/           # Index, detail, feed parsers + IT classification
  images.py         # Thumbnail/original download and deduplication
  changes.py        # Change detection and significance rules
  ai/               # Provider fallback, cache, budgets, classification,
                    #   spec extraction, scan summaries, price estimates, Q&A
  notify/           # SMTP and log-only notifiers, message templates
  pipeline.py       # Orchestration: discovery, change scan, finalization
  scheduler.py      # In-process APScheduler (discovery, scans, reminders)
  web/app.py        # Flask UI (dashboard, auctions, lots, compare, changes, ask)
  report.py         # Standalone HTML snapshot reports
  archive.py        # Archive/export functionality
```

## Configuration

Configuration is layered, with later sources overriding earlier ones:

1. **Built-in defaults** in `config.py`
2. **`config.yaml`** — non-secret behavioural settings
3. **`.env`** — secrets (SMTP passwords, AI API keys)
4. **Real environment variables** — for Docker/Kubernetes
5. **`AT__SECTION__KEY` overrides** — e.g. `AT__WEB__PAGE_SIZE=250`

See [`.env.example`](.env.example) for all available environment variables and [`config.yaml`](config.yaml) for all behavioural settings.

### Adding Custom AI Providers

Any OpenAI-compatible or Anthropic-compatible API (e.g. Groq, DeepSeek, Together, Mistral, Local vLLM, LM Studio, Ollama) can be configured in [`config.yaml`](config.yaml) under `ai.providers`:

```yaml
ai:
  providers:
    # Example: Groq (ultra-fast inference)
    - name: "groq"
      kind: "openai"                            # "openai" or "anthropic"
      base_url: "https://api.groq.com/openai/v1"
      model: "llama-3.3-70b-versatile"
      api_key_env: "GROQ_API_KEY"              # Environment variable in .env
      enabled: true

    # Example: Local vLLM / LM Studio without authentication
    - name: "local-vllm"
      kind: "openai"
      base_url: "http://host.docker.internal:8000/v1"
      model: "mistralai/Mistral-7B-Instruct-v0.3"
      require_api_key: false
      enabled: true
```

Then assign your provider to any tasks in `ai.tasks`:

```yaml
ai:
  tasks:
    classify: ["groq", "openai", "openrouter", "ollama", "anthropic"]
    extract_specs: ["groq", "openai", "openrouter", "ollama", "anthropic"]
    summarize_scan: ["groq", "openai", "openrouter", "anthropic", "ollama"]
    estimate_price: ["deepseek", "openai-reasoning", "anthropic", "openrouter"]
```

Add your API key into `.env`:

```bash
GROQ_API_KEY=gsk_your_groq_api_key
```

### Key settings

| Setting | Default | Description |
|---------|---------|-------------|
| `WEB_PORT` | `8080` | Host port for the web UI |
| `TZ` | `Australia/Perth` | Timezone for scheduler and logs |
| `SMTP_HOST` | — | SMTP server for notifications (log-only if unset) |
| `OPENAI_API_KEY` | — | OpenAI API key for AI tasks (fail-open if unset) |
| `GROQ_API_KEY` | — | Optional custom Groq API key |
| `DEEPSEEK_API_KEY` | — | Optional custom DeepSeek API key |
| `AT__FETCH__CLIENT` | `auto` | `http`, `playwright`, or `auto` (HTTP first, Playwright fallback) |
| `AT__SCHEDULE__CHANGE_EVERY_HOURS` | `6` | Hours between change scans |
| `AT__WEB__PAGE_SIZE` | `100` | Lots per page in the web UI |

## Docker

The image is published to `ghcr.io/uri-travoski/ross-auction-tracker:latest`.

- **Base:** Python 3.11-slim
- **Includes:** Playwright + Chromium binaries (for JavaScript-rendered pages)
- **Runs as:** Non-root user (uid 1000)
- **Entrypoint:** `tini` for clean signal handling
- **Default command:** `python -m auction_tracker serve` (web UI + scheduler)
- **Healthcheck:** `curl http://127.0.0.1:8080/healthz`
- **Port:** 8080
- **Volumes:**
  - `.:/app` (Mounts project directory to container so `config.yaml` and customizations are live. If `config.yaml` does not exist, the container automatically instantiates it on first run.)
  - `./data:/app/data` (Persistent state: SQLite db, images, reports, logs)

### One-shot commands

```bash
docker compose run --rm onetime discover     # run a discovery scan now
docker compose run --rm onetime scan         # run a change scan now
docker compose run --rm onetime heartbeat    # run final-stretch polling
docker compose run --rm onetime finalize     # finalize closed auctions
docker compose run --rm onetime status       # show config and health
docker compose run --rm onetime test-email   # send a test email
docker compose run --rm onetime ask "HP ZBook G8"  # AI price estimate
```

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run the full suite (offline, uses captured fixtures)
python runtests.py

# Or with pytest directly
python -m pytest tests/ -q

# Run a single module
python -m pytest tests/test_index_page.py -v

# Live tests (hits the real site — gated behind AUCTION_TRACKER_LIVE=1)
AUCTION_TRACKER_LIVE=1 python -m pytest tests/ -k live
```

The test suite uses captured fixtures from `tests/fixtures/` and does not touch the network by default. All 163 tests pass both locally and inside the Docker image.

## CLI

```bash
python -m auction_tracker serve          # web UI + scheduler (container default)
python -m auction_tracker discover       # one-shot discovery scan
python -m auction_tracker scan           # one-shot change scan
python -m auction_tracker heartbeat      # final-stretch polling + reminders
python -m auction_tracker finalize       # finalize closed auctions
python -m auction_tracker status         # config + data + health summary
python -m auction_tracker test-email     # send a test notification
python -m auction_tracker ask "HP ZBook" # AI price estimate
python -m auction_tracker classify URL   # explain how an auction is classified
python -m auction_tracker dump           # dump the SQLite db as JSON
```

## Web UI routes

| Route | Description |
|-------|-------------|
| `/` | Dashboard — summary of tracked auctions |
| `/auctions` | All auctions with search and filters |
| `/auction/<id>` | Single auction with lot catalogue |
| `/lot/<id>` | Single lot with bid history and images |
| `/image/<id>` | Stored image |
| `/lots` | All lots with search and filters |
| `/compare` | Compare lots side by side |
| `/changes` | Change history across all auctions |
| `/ask` | Ask the AI a question |
| `/status` | Config + data + health summary |
| `/reports/` | Generated HTML reports |
| `/healthz` | Health check (no auth) |

## First-run checklist

1. Copy `.env.example` to `.env` and fill in SMTP credentials (or leave blank for log-only mode).
2. Optionally add AI API keys (OpenAI, Anthropic, OpenRouter, or Ollama).
3. Run `docker compose up -d`.
4. Check `http://localhost:8080/healthz` returns `ok`.
5. Check `docker compose logs -f auction-tracker` for scheduler startup.
6. Run `docker compose run --rm onetime discover` to trigger the first discovery scan immediately.
7. Browse to `http://localhost:8080` to see tracked auctions.

## License

Private project. Not for redistribution.
