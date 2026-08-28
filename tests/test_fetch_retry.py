"""Fetcher retry and parity tests.

These exercise the retry policy in ``Fetcher.fetch`` without touching the
network, by subclassing the base fetcher and returning canned responses /
raising canned errors.
"""
from __future__ import annotations

import pytest

from auction_tracker.fetch import FetchError, FetchResult
from auction_tracker.fetch.base import Fetcher
from auction_tracker.fetch.http_fetcher import HttpFetcher


class _ScriptedFetcher(Fetcher):
    """Returns canned responses in sequence, recording sleep calls.

    Pass a list of (status, text) tuples or Exception instances. Each call to
    ``_fetch_once`` consumes the next entry.
    """

    name = "scripted"

    def __init__(self, config, *, script):
        super().__init__(config)
        self._script = list(script)
        self._idx = 0
        self.sleeps: list[float] = []
        self.attempts = 0

    def _fetch_once(self, url: str, *, binary: bool = False) -> FetchResult:
        self.attempts += 1
        if self._idx >= len(self._script):
            # Default to a 200 if we run out of script.
            return FetchResult(url=url, status=200, text="ok", fetcher=self.name)
        entry = self._script[self._idx]
        self._idx += 1
        if isinstance(entry, Exception):
            raise entry
        status, text = entry
        return FetchResult(url=url, status=status, text=text, fetcher=self.name)

    def _be_polite(self) -> None:
        # Override to record rather than actually sleep.
        if self.delay > 0:
            self.sleeps.append(self.delay)


def test_retry_on_502_then_succeeds(config):
    fetcher = _ScriptedFetcher(config, script=[(502, ""), (200, "ok")])
    fetcher.max_attempts = 3
    result = fetcher.fetch("https://x")
    assert result.ok
    assert result.text == "ok"
    assert fetcher.attempts == 2


def test_retry_on_503_then_succeeds(config):
    fetcher = _ScriptedFetcher(config, script=[(503, ""), (200, "ok")])
    fetcher.max_attempts = 3
    assert fetcher.fetch("https://x").ok


def test_retry_on_504_then_succeeds(config):
    fetcher = _ScriptedFetcher(config, script=[(504, ""), (200, "ok")])
    fetcher.max_attempts = 3
    assert fetcher.fetch("https://x").ok


def test_no_retry_on_404(config):
    fetcher = _ScriptedFetcher(config, script=[(404, "")])
    fetcher.max_attempts = 3
    with pytest.raises(FetchError) as exc:
        fetcher.fetch("https://x")
    assert exc.value.status == 404
    assert fetcher.attempts == 1, "404 must not be retried"


def test_exhausts_retries_on_persistent_503(config):
    fetcher = _ScriptedFetcher(config, script=[(503, ""), (503, ""), (503, "")])
    fetcher.max_attempts = 3
    with pytest.raises(FetchError):
        fetcher.fetch("https://x")
    assert fetcher.attempts == 3


def test_network_error_is_retried(config):
    fetcher = _ScriptedFetcher(
        config, script=[ConnectionError("boom"), (200, "ok")])
    fetcher.max_attempts = 3
    result = fetcher.fetch("https://x")
    assert result.ok
    assert fetcher.attempts == 2


def test_exponential_backoff_sleeps_grow(config, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    fetcher = _ScriptedFetcher(config, script=[(503, ""), (503, ""), (503, "")])
    fetcher.max_attempts = 3
    fetcher.base_delay = 1.0
    fetcher.backoff = 2.0
    with pytest.raises(FetchError):
        fetcher.fetch("https://x")
    # Two sleeps between three attempts: 1.0, then 2.0.
    assert len(sleeps) == 2
    assert sleeps[0] == 1.0
    assert sleeps[1] == 2.0


def test_http_fetcher_name():
    from auction_tracker.config import Config
    cfg = Config({"site": {"retry": {"max_attempts": 1}}})
    f = HttpFetcher(cfg)
    assert f.name == "http"
    f.close()
