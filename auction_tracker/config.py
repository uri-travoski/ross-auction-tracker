"""Configuration loading.

Layering, lowest precedence first:

1. ``DEFAULTS`` in this module (so the agent still runs with no config file).
2. ``config.yaml`` (path from ``$AUCTION_TRACKER_CONFIG``, else ./config.yaml).
3. ``.env`` file, then real environment variables.
4. ``AT__SECTION__KEY`` environment overrides, which map onto the nested
   config tree (``AT__WEB__PAGE_SIZE=250`` -> ``web.page_size``).

Secrets are never read from YAML: they come from the environment only. See
``AGENTS.md`` for the extension guide.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:  # PyYAML is a hard requirement in production but tests can run without it
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

ENV_PREFIX = "AT__"

DEFAULTS: dict[str, Any] = {
    "site": {
        "base_url": "https://auctions.com.au",
        "index_url": "https://auctions.com.au/auctions/online",
        "pagination_url_template": "/auctions/online/10/31/{page}?",
        "max_pages_safety_cap": 50,
        "min_auctions_expected": 1,
        "bids_feed_url": "https://static.auctions.com.au/cache/max_bids_{auction_site_id}.json",
        "lot_gallery_url": "https://static.auctions.com.au/cache/auction_lot_gallery_{lot_site_id}.json",
        "auction_gallery_url": "https://static.auctions.com.au/cache/auction_gallery_{auction_site_id}.json",
        "timezone": "Australia/Perth",
        "verify_ssl": True,
        "request_timeout_seconds": 30,
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "request_delay_seconds": 0.5,
        "retry": {
            "max_attempts": 3,
            "base_delay_seconds": 1.0,
            "backoff_multiplier": 2.0,
            "retry_on_status": [429, 500, 502, 503, 504],
        },
    },
    "fetch": {
        "client": "auto",
        "proxy_url": None,
        "playwright": {
            "browser": "chromium",
            "headless": True,
            "wait_after_load_ms": 1500,
            "accept_button_selector": "#accept-btn",
            "navigation_timeout_ms": 45000,
        },
    },
    "filter": {
        "it_keywords": [],
        "always_include_title_regex": [],
        "always_exclude_title_regex": [],
        "keep_it_lots_in_mixed_auctions": True,
        "use_ai_when_uncertain": True,
        "use_ai_always": True,
    },
    "schedule": {
        "discovery_every_hours": 24,
        "discovery_at": "06:00",
        "change_every_hours": 6,
        "final_stretch_minutes": 180,
        "final_stretch_poll_minutes": 30,
        "finalize_delay_minutes": 180,
        "heartbeat_minutes": 1,
    },
    "notify": {
        "reminders": [{"lead_minutes": 180}, {"lead_minutes": 30}],
        "on_new_auction": True,
        "on_final_stretch_entry": True,
        "on_extension": True,
        "on_finalize": True,
        "on_significant_change": True,
        "significant_change": {
            "bid_jump_pct": 20,
            "bid_jump_min_dollars": 20,
            "always_notify_lot_events": ["NEW_LOT", "REMOVED", "DESCRIPTION_CHANGED"],
        },
        "max_emails_per_hour": 20,
    },
    "email": {
        "enabled": True,
        "security": "starttls",
        "timeout_seconds": 30,
        "subject_prefix": "[Ross Auctions]",
    },
    "images": {
        "download": True,
        "download_thumbnails": True,
        "download_originals": True,
        "max_images_per_lot": 40,
        "max_bytes_per_image": 26214400,
        "dedupe_by_content_hash": True,
    },
    "ai": {
        "enabled": True,
        "providers": [],
        "tasks": {},
        "cache_enabled": True,
        "cache_ttl_days": 30,
        "max_calls_per_cycle": 250,
        "max_spec_extractions_per_cycle": 150,
        "fail_open": True,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "page_size": 100,
        "page_size_options": [25, 50, 100, 250, 500],
        "title": "Ross Auction IT Tracker",
    },
    "storage": {
        "data_dir": "./data",
        "sqlite_path": "./data/auctions.db",
        "images_dir": "./data/images",
        "reports_dir": "./data/reports",
        "logs_dir": "./data/logs",
        "archive_path": "./data/auction-tracker-archive.zip",
        "purge_reports_older_than_days": 365,
    },
    "logging": {"level": "INFO", "format": "text", "file_enabled": True},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str) -> Any:
    """Turn an environment string into a bool/int/float/list/dict where obvious."""
    text = raw.strip()
    low = text.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if low in {"null", "none", ""}:
        return None
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _apply_env_overrides(tree: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    """Apply ``AT__A__B=value`` overrides onto the nested config tree."""
    out = tree
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in name[len(ENV_PREFIX) :].split("__") if p]
        if not path:
            continue
        patch: dict[str, Any] = {}
        cursor = patch
        for part in path[:-1]:
            cursor[part] = {}
            cursor = cursor[part]
        cursor[path[-1]] = _coerce(value)
        out = _deep_merge(out, patch)
    return out


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env loader. Real environment variables always win.

    Tolerates the informal notation people put in these files, e.g.
    ``- **GITHUB_TOKEN** ghp_xxx`` or ``export KEY=value``.
    """
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lstrip("-* \t")
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip("*").strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip().strip("*").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_list(name: str, default: Iterable[str] = ()) -> list[str]:
    """Read a comma/semicolon/whitespace-separated env var into a list."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return list(default)
    parts = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",")]
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# Typed views over the config tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AIProvider:
    """One configured AI endpoint.

    ``kind`` selects the wire protocol: ``openai`` for the widely-cloned
    ``/chat/completions`` shape (OpenAI, OpenRouter, Groq, Together, vLLM,
    LM Studio, Ollama's OpenAI shim), ``anthropic`` for ``/messages``.
    """

    name: str
    kind: str
    base_url: str
    model: str
    api_key_env: str = ""
    api_key: str = ""
    enabled: bool = True
    require_api_key: bool = True
    max_tokens: int = 1500
    temperature: float = 0.0
    timeout_seconds: int = 60
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if not self.base_url or not self.model:
            return False
        return bool(self.api_key) or not self.require_api_key


@dataclass(frozen=True)
class EmailSettings:
    enabled: bool
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipients: list[str]
    reminder_recipients: list[str]
    timeout_seconds: int
    subject_prefix: str
    dry_run: bool

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.host and self.sender and self.recipients)

    def recipients_for(self, event: str) -> list[str]:
        """Recipient list for an event kind; reminders may go to extra people."""
        people = list(self.recipients)
        if event == "reminder":
            people += [r for r in self.reminder_recipients if r not in people]
        return people


class Config:
    """Read-only accessor over the merged configuration tree."""

    def __init__(self, tree: dict[str, Any], source: Path | None = None) -> None:
        self._tree = tree
        self.source = source

    # -- generic access ---------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        """Fetch by dotted path, e.g. ``config.get("web.page_size", 100)``."""
        cursor: Any = self._tree
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return dict(value) if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._tree, default=str))

    # -- convenience ------------------------------------------------------
    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(str(self.get("site.timezone", "Australia/Perth")))

    def path(self, key: str) -> Path:
        """Resolve a ``storage.*`` path relative to the project root."""
        raw = str(self.get(f"storage.{key}", f"./data/{key}"))
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def root(self) -> Path:
        return Path(os.environ.get("AUCTION_TRACKER_ROOT", ".")).resolve()

    def ensure_dirs(self) -> None:
        for key in ("data_dir", "images_dir", "reports_dir", "logs_dir"):
            self.path(key).mkdir(parents=True, exist_ok=True)

    # -- AI ---------------------------------------------------------------
    @property
    def ai_providers(self) -> list[AIProvider]:
        providers: list[AIProvider] = []
        for raw in self.get("ai.providers", []) or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            key_env = str(raw.get("api_key_env") or "")
            providers.append(
                AIProvider(
                    name=str(raw["name"]),
                    kind=str(raw.get("kind", "openai")).lower(),
                    base_url=str(raw.get("base_url", "")).rstrip("/"),
                    model=str(raw.get("model", "")),
                    api_key_env=key_env,
                    api_key=os.environ.get(key_env, "").strip() if key_env else "",
                    enabled=bool(raw.get("enabled", True)),
                    require_api_key=bool(raw.get("require_api_key", True)),
                    max_tokens=int(raw.get("max_tokens", 1500)),
                    temperature=float(raw.get("temperature", 0.0)),
                    timeout_seconds=int(raw.get("timeout_seconds", 60)),
                    extra_headers=dict(raw.get("extra_headers", {}) or {}),
                )
            )
        return providers

    def providers_for_task(self, task: str) -> list[AIProvider]:
        """Available providers for a task, in configured preference order."""
        by_name = {p.name: p for p in self.ai_providers}
        names = self.get(f"ai.tasks.{task}.providers", None)
        if not names:
            names = list(by_name)
        chain = [by_name[n] for n in names if n in by_name]
        # Anything not named for this task acts as a last-resort fallback.
        chain += [p for p in by_name.values() if p not in chain]
        return [p for p in chain if p.available]

    # -- email ------------------------------------------------------------
    @property
    def email(self) -> EmailSettings:
        default_from = os.environ.get("SMTP_USERNAME", "")
        return EmailSettings(
            enabled=bool(self.get("email.enabled", True)),
            host=os.environ.get("SMTP_HOST", "").strip(),
            port=int(os.environ.get("SMTP_PORT", "587") or 587),
            security=os.environ.get(
                "SMTP_SECURITY", str(self.get("email.security", "starttls"))
            ).strip().lower(),
            username=os.environ.get("SMTP_USERNAME", "").strip(),
            password=os.environ.get("SMTP_PASSWORD", ""),
            sender=os.environ.get("SMTP_FROM", default_from).strip(),
            recipients=env_list("SMTP_RECIPIENTS"),
            reminder_recipients=env_list("SMTP_REMINDER_RECIPIENTS"),
            timeout_seconds=int(self.get("email.timeout_seconds", 30)),
            subject_prefix=str(self.get("email.subject_prefix", "[Ross Auctions]")),
            dry_run=os.environ.get("NOTIFY_DRY_RUN", "0").strip() in {"1", "true", "yes"},
        )

    @property
    def reminder_lead_minutes(self) -> list[int]:
        leads: list[int] = []
        for entry in self.get("notify.reminders", []) or []:
            if isinstance(entry, dict) and entry.get("lead_minutes") is not None:
                leads.append(int(entry["lead_minutes"]))
            elif isinstance(entry, (int, float, str)):
                try:
                    leads.append(int(entry))
                except (TypeError, ValueError):
                    continue
        # Longest lead first so a missed tick sends the earlier reminder first.
        return sorted({m for m in leads if m > 0}, reverse=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_CACHE: Config | None = None


def load_config(path: str | os.PathLike[str] | None = None, *, reload: bool = False) -> Config:
    """Load (and memoise) the configuration."""
    global _CACHE
    if _CACHE is not None and not reload and path is None:
        return _CACHE

    load_dotenv(os.environ.get("AUCTION_TRACKER_ENV_FILE", ".env"))

    candidate = path or os.environ.get("AUCTION_TRACKER_CONFIG") or "config.yaml"
    cfg_path = Path(candidate)

    if cfg_path.is_dir():
        raise ValueError(
            f"{cfg_path} is a directory, not a file. If this was created automatically "
            f"by Docker when volume-mounting a missing file, remove the directory with "
            f"'rmdir {cfg_path}' and restart."
        )

    if not cfg_path.is_file():
        # Automatically instantiate config.yaml from the template if it does not exist
        template_candidates = [
            Path(__file__).parent / "config.default.yaml",
            Path("/etc/auction-tracker/config.default.yaml"),
            Path(__file__).parent.parent / "config.yaml",
        ]
        for template in template_candidates:
            if template.is_file() and template.resolve() != cfg_path.resolve():
                try:
                    cfg_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, cfg_path)
                    break
                except (OSError, PermissionError):
                    break

    tree = dict(DEFAULTS)
    if cfg_path.is_file():
        if yaml is None:  # pragma: no cover
            raise RuntimeError("PyYAML is required to read %s" % cfg_path)
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{cfg_path} must contain a YAML mapping at the top level")
        tree = _deep_merge(tree, loaded)
    else:
        cfg_path = None  # type: ignore[assignment]

    tree = _apply_env_overrides(tree, dict(os.environ))
    config = Config(tree, source=cfg_path)
    if path is None:
        _CACHE = config
    return config
