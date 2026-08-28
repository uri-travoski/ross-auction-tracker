"""Downloading and storing auction photos.

Requirement: keep every thumbnail *and* every original photo, permanently.
The site serves images from a Thumbor-style CDN with signed URLs, so a given
photo exists at several renditions:

* ``.../60x60/...``     gallery strip thumbnail
* ``.../300x200/...``   lot/card thumbnail
* ``.../1500x1000/...`` the original, from the per-lot gallery feed

Signatures cannot be forged, so we save exactly the renditions the site
publishes. Files are laid out under ``storage.images_dir`` as::

    <auction-key>/<lot-number>/<kind>-<site-image-id>.jpg
    <auction-key>/auction/<kind>-<site-image-id>.jpg

Every file is recorded in the ``images`` table with its SHA-256, so re-scanning
an auction every six hours re-downloads nothing: a URL already on disk is
skipped, and an identical payload that appears under a new URL is linked to the
existing file instead of being stored twice.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from .config import Config
from .fetch import Fetcher, FetchError
from .logging_setup import get_logger
from .models import Auction, Lot
from .selectors import PATTERNS
from .store import Store
from .util import sha256_bytes, slugify

log = get_logger(__name__)

THUMBNAIL = "thumbnail"
ORIGINAL = "original"
AUCTION = "auction"

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}

# The 1x1 placeholders the site uses for lazy loading; never worth storing.
_PLACEHOLDER_MARKERS = ("spacer-3x2.gif", "blank.gif", "/img/spacer", "data:image")


def is_placeholder(url: str) -> bool:
    lowered = (url or "").lower()
    return not lowered or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def site_image_id(url: str) -> str:
    """Extract the site's image id from ``/public/auctions/14704/4203183.jpg``."""
    match = re.search(PATTERNS["image_path_ids"], url or "")
    return match.group(2) if match else ""


def rendition(url: str) -> str:
    """The size segment of a CDN URL, e.g. ``1500x1000``."""
    match = re.search(r"/(\d{2,5}x\d{2,5})/", url or "")
    return match.group(1) if match else ""


def _extension(url: str, content_type: str) -> str:
    for prefix, ext in _EXT_BY_TYPE.items():
        if content_type.lower().startswith(prefix):
            return ext
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix if suffix in set(_EXT_BY_TYPE.values()) else ".jpg"


class ImageDownloader:
    """Fetches and stores images, keeping the ``images`` table in step."""

    def __init__(self, config: Config, store: Store, fetcher: Fetcher) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.root = config.path("images_dir")
        self.enabled = bool(config.get("images.download", True))
        self.want_thumbnails = bool(config.get("images.download_thumbnails", True))
        self.want_originals = bool(config.get("images.download_originals", True))
        self.max_per_lot = int(config.get("images.max_images_per_lot", 40))
        self.max_bytes = int(config.get("images.max_bytes_per_image", 25 * 1024 * 1024))
        self.dedupe = bool(config.get("images.dedupe_by_content_hash", True))
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0

    # ==================================================================
    # Public API
    # ==================================================================
    def download_auction_images(self, auction: Auction) -> int:
        """Auction-level artwork (the card/primary image)."""
        if not self.enabled or not self.want_thumbnails:
            return 0
        count = 0
        if not is_placeholder(auction.thumbnail_url):
            if self._store_one(
                auction.thumbnail_url,
                kind=AUCTION,
                directory=self._auction_dir(auction) / "auction",
                auction_id=auction.id,
            ):
                count += 1
        return count

    def download_lot_images(self, auction: Auction, lot: Lot) -> int:
        """Every thumbnail and original photo for one lot."""
        if not self.enabled:
            return 0
        directory = self._auction_dir(auction) / f"lot-{slugify(lot.lot_number, 20)}"
        count = 0

        if self.want_thumbnails:
            thumbs = [lot.thumbnail_url, *lot.thumbnail_urls]
            for url in self._unique(thumbs, self.max_per_lot + 1):
                if self._store_one(
                    url,
                    kind=THUMBNAIL,
                    directory=directory,
                    auction_id=auction.id,
                    lot_id=lot.id,
                ):
                    count += 1

        if self.want_originals:
            for url in self._unique(lot.image_urls, self.max_per_lot):
                if self._store_one(
                    url,
                    kind=ORIGINAL,
                    directory=directory,
                    auction_id=auction.id,
                    lot_id=lot.id,
                ):
                    count += 1
        return count

    def download_for_lots(self, auction: Auction, lots: Iterable[Lot]) -> int:
        return sum(self.download_lot_images(auction, lot) for lot in lots)

    # ==================================================================
    # Internals
    # ==================================================================
    def _auction_dir(self, auction: Auction) -> Path:
        key = auction.site_id or auction.slug or str(auction.id or "unknown")
        return self.root / slugify(key, 60)

    @staticmethod
    def _unique(urls: Iterable[str], limit: int) -> list[str]:
        out: list[str] = []
        for url in urls or []:
            if is_placeholder(url) or url in out:
                continue
            out.append(url)
            if len(out) >= limit:
                break
        return out

    def _store_one(
        self,
        url: str,
        *,
        kind: str,
        directory: Path,
        auction_id: int | None = None,
        lot_id: int | None = None,
    ) -> bool:
        """Download one image unless we already have it. True if bytes landed."""
        existing = self.store.image_by_url(url)
        if existing and existing.get("local_path"):
            absolute = self.root / existing["local_path"]
            if absolute.is_file():
                self.skipped += 1
                # Attach it to the lot/auction if it was recorded before they existed.
                if (lot_id and not existing.get("lot_id")) or (
                    auction_id and not existing.get("auction_id")
                ):
                    self.store.record_image(
                        source_url=url,
                        kind=kind,
                        auction_id=auction_id,
                        lot_id=lot_id,
                        site_image_id=existing.get("site_image_id", ""),
                        local_path=existing["local_path"],
                        sha256=existing.get("sha256", ""),
                        byte_size=int(existing.get("byte_size") or 0),
                        content_type=existing.get("content_type", ""),
                    )
                return False

        try:
            result = self.fetcher.get_bytes(url)
        except FetchError as exc:
            self.failed += 1
            log.warning("image download failed", extra={"url": url, "error": exc.message})
            self.store.record_image(
                source_url=url,
                kind=kind,
                auction_id=auction_id,
                lot_id=lot_id,
                site_image_id=site_image_id(url),
                error=exc.message[:400],
            )
            return False

        payload = result.content
        if not payload:
            self.failed += 1
            self.store.record_image(
                source_url=url,
                kind=kind,
                auction_id=auction_id,
                lot_id=lot_id,
                site_image_id=site_image_id(url),
                error="empty response",
            )
            return False
        if len(payload) > self.max_bytes:
            self.failed += 1
            log.warning(
                "image exceeds max_bytes_per_image, not stored",
                extra={"url": url, "bytes": len(payload)},
            )
            self.store.record_image(
                source_url=url,
                kind=kind,
                auction_id=auction_id,
                lot_id=lot_id,
                site_image_id=site_image_id(url),
                error=f"too large: {len(payload)} bytes",
            )
            return False

        digest = sha256_bytes(payload)

        # Same bytes already on disk under another URL (e.g. the same photo
        # reached via the lot thumb and the gallery strip): point at that file.
        if self.dedupe:
            twin = self.store.image_by_hash(digest)
            if twin and (self.root / twin["local_path"]).is_file():
                self.store.record_image(
                    source_url=url,
                    kind=kind,
                    auction_id=auction_id,
                    lot_id=lot_id,
                    site_image_id=site_image_id(url),
                    local_path=twin["local_path"],
                    sha256=digest,
                    byte_size=len(payload),
                    content_type=result.content_type,
                )
                self.skipped += 1
                return False

        image_id = site_image_id(url) or digest[:12]
        size = rendition(url)
        name = f"{kind}-{image_id}"
        if size:
            name += f"-{size}"
        path = directory / f"{name}{_extension(url, result.content_type)}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Write via a temp file so a crash cannot leave a truncated image.
            temp = path.with_suffix(path.suffix + ".part")
            temp.write_bytes(payload)
            os.replace(temp, path)
        except OSError as exc:
            self.failed += 1
            log.error("could not write image", extra={"path": str(path), "error": str(exc)})
            self.store.record_image(
                source_url=url,
                kind=kind,
                auction_id=auction_id,
                lot_id=lot_id,
                site_image_id=image_id,
                error=f"write failed: {exc}",
            )
            return False

        relative = path.relative_to(self.root).as_posix()
        self.store.record_image(
            source_url=url,
            kind=kind,
            auction_id=auction_id,
            lot_id=lot_id,
            site_image_id=image_id,
            local_path=relative,
            sha256=digest,
            byte_size=len(payload),
            content_type=result.content_type,
        )
        self.downloaded += 1
        log.debug(
            "image stored",
            extra={"path": relative, "bytes": len(payload), "kind": kind},
        )
        return True

    def summary(self) -> dict[str, int]:
        return {
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
        }
