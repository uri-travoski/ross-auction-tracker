"""Logging configuration: stdout (for ``docker logs``) plus a rotating file."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including any ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable single line, with ``extra=`` fields appended."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def setup_logging(
    level: str = "INFO",
    fmt: str = "text",
    log_dir: Path | None = None,
    file_enabled: bool = True,
) -> None:
    """Configure the root logger. Safe to call more than once."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(level.upper())
        return

    formatter: logging.Formatter = (
        JsonFormatter() if fmt.lower() == "json" else TextFormatter()
    )
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if file_enabled and log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                log_dir / "auction-tracker.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
        except OSError as exc:  # read-only volume, etc. — stdout still works
            root.warning("file logging disabled: %s", exc)

    # These libraries are chatty at INFO and add nothing useful here.
    for noisy in ("urllib3", "apscheduler", "werkzeug", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
