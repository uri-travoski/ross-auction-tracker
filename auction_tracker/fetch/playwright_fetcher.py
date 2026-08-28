"""Headless-browser fetcher.

Used when a page needs JavaScript. It also dismisses the terms-and-conditions
modal the site shows on auction detail pages, which blocks interaction in a
real browser (it does not affect raw HTTP).

The browser is started lazily on first use and reused for the whole cycle, so
nothing is paid for it when the HTTP backend is doing the work.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..logging_setup import get_logger
from .base import Fetcher, FetchResult

log = get_logger(__name__)


class PlaywrightFetcher(Fetcher):
    name = "playwright"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        opts = config.section("fetch").get("playwright", {}) or {}
        self.browser_name = str(opts.get("browser", "chromium"))
        self.headless = bool(opts.get("headless", True))
        self.wait_after_load_ms = int(opts.get("wait_after_load_ms", 1500))
        self.accept_selector = str(opts.get("accept_button_selector", "#accept-btn"))
        self.navigation_timeout_ms = int(opts.get("navigation_timeout_ms", 45000))
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    # -- lifecycle --------------------------------------------------------
    def _ensure_browser(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "playwright is not installed. `pip install playwright && "
                "playwright install chromium`, or set fetch.client: http"
            ) from exc

        self._playwright = sync_playwright().start()
        launcher = getattr(self._playwright, self.browser_name)
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            # Required in containers, where /dev/shm is small and there is no
            # user namespace available to the sandbox.
            "args": ["--disable-dev-shm-usage", "--no-sandbox"],
        }
        if self.proxy_url:
            launch_kwargs["proxy"] = {"server": self.proxy_url}
        self._browser = launcher.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            ignore_https_errors=not self.verify_ssl,
            viewport={"width": 1440, "height": 1000},
            locale="en-AU",
        )
        self._context.set_default_navigation_timeout(self.navigation_timeout_ms)
        log.info(
            "playwright browser started",
            extra={"browser": self.browser_name, "headless": self.headless},
        )
        return self._context

    def close(self) -> None:
        for attr in ("_context", "_browser", "_playwright"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                obj.stop() if attr == "_playwright" else obj.close()
            except Exception as exc:  # pragma: no cover
                log.debug("error closing %s: %s", attr, exc)
            setattr(self, attr, None)

    # -- fetching ---------------------------------------------------------
    def _fetch_once(self, url: str, *, binary: bool = False) -> FetchResult:
        context = self._ensure_browser()
        if binary:
            # Images are plain GETs; go through the browser's request context so
            # cookies and TLS settings are shared.
            response = context.request.get(url, timeout=self.timeout * 1000)
            return FetchResult(
                url=url,
                status=response.status,
                content=response.body(),
                content_type=response.headers.get("content-type", ""),
            )

        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else 0

            # The T&Cs modal blocks the catalogue; accept it if present.
            if self.accept_selector:
                try:
                    button = page.locator(self.accept_selector).first
                    if button.count() and button.is_visible():
                        button.click(timeout=3000)
                        page.wait_for_timeout(250)
                except Exception:
                    pass  # not present on every page; not fatal

            # Let lazy-loaded lot rows and the JS pager settle.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            self._scroll_to_bottom(page)
            if self.wait_after_load_ms:
                page.wait_for_timeout(self.wait_after_load_ms)

            return FetchResult(
                url=page.url,
                status=status or 200,
                text=page.content(),
                content_type="text/html",
            )
        finally:
            page.close()

    @staticmethod
    def _scroll_to_bottom(page: Any, steps: int = 12) -> None:
        """Trigger lazy-loading images/rows by walking down the page."""
        try:
            for _ in range(steps):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(120)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
