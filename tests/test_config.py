"""Config loader tests: layering, env overrides, AI providers, email settings."""
from __future__ import annotations

import os

import pytest

from auction_tracker.config import (
    AIProvider,
    Config,
    DEFAULTS,
    _apply_env_overrides,
    _coerce,
    _deep_merge,
    env_list,
    load_config,
)


def test_deep_merge_preserves_nested_keys():
    base = {"a": {"b": 1, "c": 2}, "x": 10}
    over = {"a": {"c": 3, "d": 4}}
    out = _deep_merge(base, over)
    assert out == {"a": {"b": 1, "c": 3, "d": 4}, "x": 10}


def test_coerce_bools():
    assert _coerce("true") is True
    assert _coerce("yes") is True
    assert _coerce("on") is True
    assert _coerce("false") is False
    assert _coerce("no") is False
    assert _coerce("off") is False


def test_coerce_numbers():
    assert _coerce("42") == 42
    assert _coerce("3.14") == 3.14
    assert _coerce("not a number") == "not a number"


def test_coerce_none():
    assert _coerce("null") is None
    assert _coerce("none") is None
    assert _coerce("") is None


def test_coerce_json_list():
    assert _coerce('["a","b"]') == ["a", "b"]


def test_apply_env_overrides_nested():
    tree = {"web": {"port": 8000, "host": "0.0.0.0"}}
    out = _apply_env_overrides(tree, {"AT__WEB__PORT": "8080"})
    assert out["web"]["port"] == 8080
    assert out["web"]["host"] == "0.0.0.0"


def test_apply_env_overrides_creates_new_keys():
    tree = {"web": {"port": 8000}}
    out = _apply_env_overrides(tree, {"AT__AI__ENABLED": "false"})
    assert out["ai"]["enabled"] is False


def test_env_list_comma_separated(monkeypatch):
    monkeypatch.setenv("X", "a@x.com,b@x.com,c@x.com")
    assert env_list("X") == ["a@x.com", "b@x.com", "c@x.com"]


def test_env_list_semicolon_separated(monkeypatch):
    monkeypatch.setenv("X", "a@x.com;b@x.com")
    assert env_list("X") == ["a@x.com", "b@x.com"]


def test_env_list_empty(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert env_list("X") == []


def test_env_list_default(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert env_list("X", default=["d"]) == ["d"]


def test_defaults_have_required_sections():
    for section in ("site", "fetch", "filter", "schedule", "notify", "email",
                    "images", "ai", "web", "storage", "logging"):
        assert section in DEFAULTS


def test_defaults_web_port_is_8080():
    assert DEFAULTS["web"]["port"] == 8080


def test_config_get_dotted_path():
    cfg = Config({"web": {"port": 8080, "host": "0.0.0.0"}})
    assert cfg.get("web.port") == 8080
    assert cfg.get("web.host") == "0.0.0.0"
    assert cfg.get("web.missing", "fallback") == "fallback"


def test_config_section_returns_dict():
    cfg = Config({"site": {"base_url": "https://x"}})
    assert cfg.section("site") == {"base_url": "https://x"}
    assert cfg.section("missing") == {}


def test_ai_provider_available_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {"providers": [{
            "name": "openai", "kind": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY",
        }]}
    })
    providers = cfg.ai_providers
    assert len(providers) == 1
    assert providers[0].available is True
    assert providers[0].api_key == "sk-test"


def test_ai_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config({
        "ai": {"providers": [{
            "name": "openai", "kind": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY",
        }]}
    })
    assert cfg.ai_providers[0].available is False


def test_ai_provider_no_key_required():
    cfg = Config({
        "ai": {"providers": [{
            "name": "ollama", "kind": "openai",
            "base_url": "http://localhost:11434/v1",
            "model": "llama3.1", "require_api_key": False,
        }]}
    })
    assert cfg.ai_providers[0].available is True


def test_providers_for_task_respects_order(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    cfg = Config({
        "ai": {
            "providers": [
                {"name": "openai", "kind": "openai", "base_url": "https://x",
                 "model": "m", "api_key_env": "OPENAI_API_KEY"},
                {"name": "anthropic", "kind": "anthropic", "base_url": "https://y",
                 "model": "m", "api_key_env": "ANTHROPIC_API_KEY"},
            ],
            "tasks": {"classify": {"providers": ["anthropic", "openai"]}},
        }
    })
    chain = cfg.providers_for_task("classify")
    assert chain[0].name == "anthropic"
    assert chain[1].name == "openai"


def test_providers_for_task_falls_back_to_all(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {
            "providers": [
                {"name": "openai", "kind": "openai", "base_url": "https://x",
                 "model": "m", "api_key_env": "OPENAI_API_KEY"},
            ],
        }
    })
    chain = cfg.providers_for_task("estimate_price")
    assert len(chain) == 1
    assert chain[0].name == "openai"


def test_email_settings_configured(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("SMTP_FROM", "from@x.com")
    monkeypatch.setenv("SMTP_RECIPIENTS", "a@x.com,b@x.com")
    cfg = Config({})
    email = cfg.email
    assert email.configured is True
    assert email.recipients == ["a@x.com", "b@x.com"]


def test_email_settings_unconfigured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_RECIPIENTS", raising=False)
    cfg = Config({})
    assert cfg.email.configured is False


def test_reminder_lead_minutes_sorted_desc():
    cfg = Config({"notify": {"reminders": [
        {"lead_minutes": 30}, {"lead_minutes": 180}, {"lead_minutes": 180},
    ]}})
    assert cfg.reminder_lead_minutes == [180, 30]


def test_load_config_with_yaml(tmp_path, monkeypatch):
    yaml_path = tmp_path / "test.yaml"
    yaml_path.write_text("web:\n  port: 9090\n")
    monkeypatch.setenv("AUCTION_TRACKER_CONFIG", str(yaml_path))
    cfg = load_config(reload=True)
    assert cfg.get("web.port") == 9090
    # Defaults still present.
    assert cfg.get("site.base_url") == "https://auctions.com.au"
