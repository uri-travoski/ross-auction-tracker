"""Fetcher interface shared by the HTTP and Playwright backends."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..logging_setup import get_logger

log = get_logger(__name__)


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved after all retries."""

    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{url}: {message}")
        self.url = url
        self.status = status
        self.message = message


@dataclass
class FetchResult:
    url: str
    status: int
    text: str = ""
    content: bytes = b""
    content_type: str = ""
    fetcher: str = ""
    elapsed_ms: int = 0
    attempts: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text or self.content.decode("utf-8", "replace"))


# A validator decides whether a response is usable. ``AutoFetcher`` uses it to
# tell "the page is fine" from "the page needs JavaScript".
Validator = Callable[[FetchResult], bool]


class Fetcher:
    """Base class holding the shared retry/politeness policy."""

    name = "base"

    def __init__(self, config: Config) -> None:
        self.config = config
        site = config.section("site")
        retry = site.get("retry", {}) or {}
        self.timeout = int(site.get("request_timeout_seconds", 30))
        self.verify_ssl = bool(site.get("verify_ssl", True))
        self.user_agent = str(site.get("user_agent", "Mozilla/5.0"))
        self.delay = float(site.get("request_delay_seconds", 0.0))
        self.max_attempts = max(1, int(retry.get("max_attempts", 3)))
        self.base_delay = float(retry.get("base_delay_seconds", 1.0))
        self.backoff = float(retry.get("backoff_multiplier", 2.0))
        self.retry_status = set(retry.get("retry_on_status", [429, 500, 502, 503, 504]))
        self.proxy_url = config.get("fetch.proxy_url") or None
        self._last_request = 0.0

    # -- to implement -----------------------------------------------------
    def _fetch_once(self, url: str, *, binary: bool = False) -> FetchResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources (browser, session)."""

    # -- shared behaviour -------------------------------------------------
    def _be_polite(self) -> None:
        if self.delay <= 0:
            return
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def fetch(
        self,
        url: str,
        *,
        binary: bool = False,
        validator: Validator | None = None,
    ) -> FetchResult:
        """Fetch a URL with retries on transient failures.

        Raises :class:`FetchError` when every attempt fails; a 4xx (other than
        429) is not retried because it will not get better.
        """
        last_error = "unknown error"
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._be_polite()
            started = time.monotonic()
            try:
                result = self._fetch_once(url, binary=binary)
            except Exception as exc:  # network error, browser crash, timeout
                last_error = f"{type(exc).__name__}: {exc}"
                result = None
            finally:
                self._last_request = time.monotonic()

            if result is not None:
                result.attempts = attempt
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                result.fetcher = self.name
                last_status = result.status
                if result.ok and (validator is None or validator(result)):
                    log.debug(
                        "fetched",
                        extra={
                            "url": url,
                            "status": result.status,
                            "ms": result.elapsed_ms,
                            "fetcher": self.name,
                            "attempt": attempt,
                        },
                    )
                    return result
                if result.ok:
                    # 200 but unusable content — let the caller decide.
                    return result
                last_error = f"HTTP {result.status}"
                if result.status not in self.retry_status:
                    break

            if attempt < self.max_attempts:
                sleep_for = self.base_delay * (self.backoff ** (attempt - 1))
                log.warning(
                    "fetch failed, retrying",
                    extra={
                        "url": url,
                        "error": last_error,
                        "attempt": attempt,
                        "sleep": round(sleep_for, 2),
                        "fetcher": self.name,
                    },
                )
                time.sleep(sleep_for)

        raise FetchError(url, last_error, last_status)

    # -- convenience ------------------------------------------------------
    def get_text(self, url: str, validator: Validator | None = None) -> str:
        return self.fetch(url, validator=validator).text

    def get_json(self, url: str) -> Any:
        """Fetch and parse JSON. Returns ``None`` if absent or unparseable."""
        try:
            result = self.fetch(url)
        except FetchError as exc:
            log.debug("json fetch failed", extra={"url": url, "error": exc.message})
            return None
        try:
            return result.json()
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("invalid json", extra={"url": url, "error": str(exc)})
            return None

    def get_bytes(self, url: str) -> FetchResult:
        return self.fetch(url, binary=True)

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
