#!/usr/bin/env python3
"""Thin wrapper around pytest so the Docker `test` profile and CI have one
canonical entry point: ``python runtests.py``.

By default the suite runs offline against the captured fixtures in
``tests/fixtures/``. To exercise the live site as well, set
``AUCTION_TRACKER_LIVE=1`` — those tests are marked ``live`` and are skipped
otherwise.
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        import pytest
    except ModuleNotFoundError:
        print(
            "pytest is not installed. Run: pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return 2

    args = [
        "tests/",
        "-q",
        # Show a short traceback for failures, skip noisy ones.
        "--tb=short",
        # Error on our own deprecated API usage, but ignore deprecation
        # warnings from third-party libraries (lxml, bs4, etc.) that we
        # cannot control.
        "-W", "error::DeprecationWarning:auction_tracker",
        "-W", "default::DeprecationWarning",
    ]
    # Allow callers to pass through extra pytest flags, e.g.
    # `python runtests.py -x` or `python runtests.py tests/test_index.py`.
    args.extend(sys.argv[1:])
    return int(pytest.main(args))


if __name__ == "__main__":
    sys.exit(main())
