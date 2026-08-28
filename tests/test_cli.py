"""CLI smoke tests: every command parses and the common ones run offline."""
from __future__ import annotations

import os

import pytest

from auction_tracker.cli import main


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point every test at a temp data dir so we never touch ./data."""
    monkeypatch.setenv("AUCTION_TRACKER_ROOT", str(tmp_path))
    monkeypatch.setenv("AUCTION_TRACKER_CONFIG", str(tmp_path / "missing.yaml"))
    # No SMTP/AI keys -> log-only / no-AI mode.
    for key in ("SMTP_HOST", "SMTP_FROM", "SMTP_RECIPIENTS",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                "WEB_USERNAME", "WEB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)


def test_cli_status(tmp_path):
    code = main(["status"])
    assert code == 0


def test_cli_init_db(tmp_path):
    code = main(["init-db"])
    assert code == 0
    # The db lives at <root>/data/auctions.db per DEFAULTS.
    assert (tmp_path / "data" / "auctions.db").exists()


def test_cli_test_email_logs_in_log_only_mode(tmp_path):
    # No SMTP configured -> log-only notifier -> test-email logs but exits 0.
    code = main(["test-email"])
    assert code == 0


def test_cli_ask_without_ai_returns_gracefully(tmp_path):
    # No AI providers configured -> ask prints a message and returns 1
    # (graceful degradation, not a crash).
    code = main(["ask", "HP", "ZBook", "G8"])
    assert code == 1


def test_cli_classify_without_ai(tmp_path):
    # classify takes a positional URL, not --url. Without network it will
    # fail to fetch, but the command should still parse and return 0 or 1.
    code = main(["classify", "https://auctions.com.au/x.html"])
    assert code in (0, 1)


def test_cli_unknown_command_exits_nonzero():
    # argparse exits with code 2 for unknown subcommands.
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code != 0


def test_cli_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_serve_flag_parsing():
    # `serve --help` should parse and exit 0 without starting the server.
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--help"])
    assert exc.value.code == 0
