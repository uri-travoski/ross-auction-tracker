"""Plain-HTTP fetcher (requests, with a urllib fallback).

Recon showed auctions.com.au renders its auction index and full lot catalogue
server-side, so this backend handles everything and is an order of magnitude
cheaper than driving a browser. The Playwright backend exists for the day that
changes.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from typing import Any

from ..config import Config
from ..logging_setup import get_logger
from .base import Fetcher, FetchResult

log = get_logger(__name__)

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore[assignment]


class HttpFetcher(Fetcher):
    name = "http"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._session: Any = None
        if requests is not None:
            self._session = requests.Session()
            self._session.headers.update(self._headers())
            if self.proxy_url:
                self._session.proxies.update(
                    {"http": self.proxy_url, "https": self.proxy_url}
                )
        elif not self.verify_ssl:
            log.warning("SSL verification is disabled")

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-AU,en;q=0.9",
            "Connection": "keep-alive",
        }

    def _fetch_once(self, url: str, *, binary: bool = False) -> FetchResult:
        if self._session is not None:
            return self._fetch_requests(url, binary=binary)
        return self._fetch_urllib(url, binary=binary)

    def _fetch_requests(self, url: str, *, binary: bool) -> FetchResult:
        response = self._session.get(
            url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True
        )
        content_type = response.headers.get("Content-Type", "")
        result = FetchResult(
            url=response.url,
            status=response.status_code,
            content_type=content_type,
            headers=dict(response.headers),
        )
        if binary:
            result.content = response.content
        else:
            # The site does not always send a charset; UTF-8 is correct here.
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = "utf-8"
            result.text = response.text
        return result

    def _fetch_urllib(self, url: str, *, binary: bool) -> FetchResult:
        context = None
        if not self.verify_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        request = urllib.request.Request(url, headers=self._headers())
        opener_args: list[Any] = []
        if self.proxy_url:
            opener_args.append(
                urllib.request.ProxyHandler(
                    {"http": self.proxy_url, "https": self.proxy_url}
                )
            )
        if context is not None:
            opener_args.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*opener_args)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                status = response.status
                headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
        result = FetchResult(
            url=url,
            status=status,
            content_type=headers.get("Content-Type", ""),
            headers=headers,
        )
        if binary:
            result.content = payload
        else:
            result.text = payload.decode("utf-8", "replace")
        return result

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
