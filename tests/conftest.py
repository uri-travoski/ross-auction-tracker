"""Shared fixtures for the ross-auction-tracker test suite.

Everything here is offline: parsers are fed the captured HTML/JSON fixtures,
the store runs against an in-memory SQLite database, and the fetcher is a
fake that returns whatever the test puts into it. No network access is
required to run `python runtests.py`.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from auction_tracker.config import Config, load_config
from auction_tracker.db import Database
from auction_tracker.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Config / store
# ---------------------------------------------------------------------------


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """A config rooted at a temp dir so no test touches the real ./data."""
    # Reload with the project's config.yaml but point storage at tmp_path.
    cfg = load_config(reload=True)
    # Mutate the underlying tree so storage paths land in tmp_path.
    tree = cfg.as_dict()
    tree["storage"]["data_dir"] = str(tmp_path)
    tree["storage"]["sqlite_path"] = str(tmp_path / "auctions.db")
    tree["storage"]["images_dir"] = str(tmp_path / "images")
    tree["storage"]["reports_dir"] = str(tmp_path / "reports")
    tree["storage"]["logs_dir"] = str(tmp_path / "logs")
    tree["logging"]["file_enabled"] = False
    tree["images"]["download"] = False  # tests never hit the network for images
    return Config(tree, source=cfg.source)


@pytest.fixture()
def store(config: Config) -> Store:
    """A fresh, migrated in-memory store."""
    db = Database(":memory:")
    db.migrate()
    return Store(db)


# ---------------------------------------------------------------------------
# Fixtures (captured live HTML/JSON)
# ---------------------------------------------------------------------------


def _read_fixture(name: str, *, binary: bool = False) -> str | bytes:
    path = FIXTURES / name
    if name.endswith(".gz"):
        data = gzip.decompress(path.read_bytes())
        return data if binary else data.decode("utf-8", "replace")
    if binary:
        return path.read_bytes()
    return path.read_text(encoding="utf-8")


@pytest.fixture()
def index_page_1_html() -> str:
    return _read_fixture("index_page_1.html")


@pytest.fixture()
def index_page_2_html() -> str:
    return _read_fixture("index_page_2.html")


@pytest.fixture()
def index_page_empty_html() -> str:
    return _read_fixture("index_page_empty.html")


@pytest.fixture()
def detail_page_it_html() -> str:
    return _read_fixture("detail_page_it.html.gz")  # type: ignore[return-value]


@pytest.fixture()
def max_bids_14704() -> list[dict[str, Any]]:
    return json.loads(_read_fixture("max_bids_14704.json"))


@pytest.fixture()
def lot_gallery_2206305() -> list[dict[str, Any]]:
    return json.loads(_read_fixture("lot_gallery_2206305.json"))


# ---------------------------------------------------------------------------
# Fake fetcher
# ---------------------------------------------------------------------------


from auction_tracker.fetch import FetchResult  # noqa: E402
from auction_tracker.fetch.base import Fetcher  # noqa: E402


class FakeFetcher(Fetcher):
    """Returns canned responses keyed by URL.

    Register a response with ``add(url, text=...)`` or ``add_bytes(url, ...)``.
    Unregistered URLs raise ``FetchError`` so a test fails loudly if the code
    under test reaches somewhere unexpected.
    """

    name = "fake"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._text: dict[str, str] = {}
        self._bytes: dict[str, bytes] = {}
        self._json: dict[str, Any] = {}
        self.calls: list[str] = []

    def add(self, url: str, *, text: str = "", status: int = 200) -> None:
        self._text[url] = text
        self._status = status

    def add_bytes(self, url: str, payload: bytes, *, content_type: str = "image/jpeg") -> None:
        self._bytes[url] = payload
        self._content_type = content_type

    def add_json(self, url: str, payload: Any) -> None:
        self._json[url] = payload

    def _fetch_once(self, url: str, *, binary: bool = False) -> FetchResult:
        self.calls.append(url)
        if url in self._json:
            return FetchResult(
                url=url, status=200, text=json.dumps(self._json[url]),
                content_type="application/json", fetcher=self.name,
            )
        if binary or url in self._bytes:
            payload = self._bytes.get(url, b"")
            return FetchResult(
                url=url, status=200, content=payload,
                content_type="image/jpeg", fetcher=self.name,
            )
        if url in self._text:
            return FetchResult(
                url=url, status=200, text=self._text[url],
                content_type="text/html", fetcher=self.name,
            )
        from auction_tracker.fetch.base import FetchError
        raise FetchError(url, f"no canned response for {url}", status=404)


@pytest.fixture()
def fake_fetcher(config: Config) -> FakeFetcher:
    return FakeFetcher(config)
