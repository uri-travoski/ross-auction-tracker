"""Fetcher factory.

``fetch.client`` in config.yaml selects the backend:

* ``http``       — requests/urllib only.
* ``playwright`` — always drive a headless browser.
* ``auto``       — HTTP first; if a page comes back unusable (a validator
  rejects it, e.g. an auction index with zero cards), transparently re-fetch it
  through Playwright. This keeps the common case fast while guaranteeing the
  crawler still works if the site becomes JavaScript-rendered.
"""

from __future__ import annotations

from ..config import Config
from ..logging_setup import get_logger
from .base import Fetcher, FetchError, FetchResult, Validator
from .http_fetcher import HttpFetcher
from .playwright_fetcher import PlaywrightFetcher

log = get_logger(__name__)

__all__ = [
    "Fetcher",
    "FetchError",
    "FetchResult",
    "Validator",
    "HttpFetcher",
    "PlaywrightFetcher",
    "AutoFetcher",
    "build_fetcher",
]


class AutoFetcher(Fetcher):
    """HTTP with a transparent Playwright fallback."""

    name = "auto"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._http = HttpFetcher(config)
        self._browser: PlaywrightFetcher | None = None
        self._browser_failed = False

    @property
    def browser(self) -> PlaywrightFetcher | None:
        if self._browser_failed:
            return None
        if self._browser is None:
            self._browser = PlaywrightFetcher(self.config)
        return self._browser

    def _fetch_once(self, url: str, *, binary: bool = False) -> FetchResult:
        # Only reached if someone calls fetch() on this class directly.
        return self._http._fetch_once(url, binary=binary)

    def fetch(
        self,
        url: str,
        *,
        binary: bool = False,
        validator: Validator | None = None,
    ) -> FetchResult:
        try:
            result = self._http.fetch(url, binary=binary, validator=validator)
            if validator is None or validator(result):
                return result
            log.info(
                "HTTP response unusable, escalating to browser", extra={"url": url}
            )
        except FetchError as exc:
            log.warning(
                "HTTP fetch failed, escalating to browser",
                extra={"url": url, "error": exc.message},
            )
            result = None

        browser = self.browser
        if browser is None:
            if result is not None:
                return result
            raise FetchError(url, "HTTP failed and Playwright is unavailable")
        try:
            return browser.fetch(url, binary=binary, validator=validator)
        except Exception as exc:
            self._browser_failed = True
            log.error(
                "browser fallback failed; continuing with HTTP only",
                extra={"url": url, "error": f"{type(exc).__name__}: {exc}"},
            )
            if result is not None:
                return result
            raise FetchError(url, f"browser fallback failed: {exc}") from exc

    def close(self) -> None:
        self._http.close()
        if self._browser is not None:
            self._browser.close()
            self._browser = None


_BACKENDS = {
    "auto": AutoFetcher,
    "http": HttpFetcher,
    "requests": HttpFetcher,
    "stdlib": HttpFetcher,
    "playwright": PlaywrightFetcher,
    "browser": PlaywrightFetcher,
}


def build_fetcher(config: Config, client: str | None = None) -> Fetcher:
    """Instantiate the configured fetcher."""
    name = (client or str(config.get("fetch.client", "auto"))).strip().lower()
    backend = _BACKENDS.get(name)
    if backend is None:
        log.warning("unknown fetch.client %r, using auto", name)
        backend = AutoFetcher
    return backend(config)
