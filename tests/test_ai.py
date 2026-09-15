"""AI provider tests: JSON extraction, response parsing, fallback, caching."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from auction_tracker.ai.providers import (
    AIError,
    AIResponse,
    ProviderClient,
    extract_json,
)
from auction_tracker.ai.tasks import AIEngine, build_comparable_terms
from auction_tracker.config import Config
from auction_tracker.models import Auction, Lot


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_markdown_fence():
    text = '```json\n{"is_it": true, "confidence": 0.9}\n```'
    assert extract_json(text) == {"is_it": True, "confidence": 0.9}


def test_extract_json_with_surrounding_prose():
    text = 'Here is my answer:\n{"is_it": true}\nHope that helps.'
    assert extract_json(text) == {"is_it": True}


def test_extract_json_array():
    assert extract_json('[1, 2, 3]') == [1, 2, 3]


def test_extract_json_garbage_returns_default():
    assert extract_json("not json at all", default=None) is None


def test_extract_json_empty():
    assert extract_json("", default=[]) == []


def test_ai_response_json_method():
    resp = AIResponse(text='{"x": 1}', provider="p", model="m")
    assert resp.json() == {"x": 1}


# ---------------------------------------------------------------------------
# Provider client — mocked HTTP
# ---------------------------------------------------------------------------


def _openai_provider(monkeypatch) -> Config:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return Config({
        "ai": {"providers": [{
            "name": "openai", "kind": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY",
            "max_tokens": 100, "temperature": 0.0,
        }]},
    })


def test_provider_client_openai_success(monkeypatch):
    cfg = _openai_provider(monkeypatch)
    provider = cfg.ai_providers[0]
    client = ProviderClient(provider)
    fake_response = {
        "choices": [{"message": {"content": '{"is_it": true}'}}],
        "model": "gpt-4o-mini",
    }
    with patch("auction_tracker.ai.providers._post_json", return_value=fake_response):
        resp = client.complete("classify this", system="you are an analyst")
    assert resp.provider == "openai"
    assert resp.model == "gpt-4o-mini"
    assert resp.json() == {"is_it": True}


def test_provider_client_openai_failure_raises(monkeypatch):
    cfg = _openai_provider(monkeypatch)
    provider = cfg.ai_providers[0]
    client = ProviderClient(provider)
    with patch("auction_tracker.ai.providers._post_json",
               side_effect=AIError("HTTP 500: boom")):
        with pytest.raises(AIError):
            client.complete("classify this")


def test_provider_client_anthropic_success(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    cfg = Config({
        "ai": {"providers": [{
            "name": "anthropic", "kind": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-3-5-sonnet", "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 100,
        }]},
    })
    provider = cfg.ai_providers[0]
    client = ProviderClient(provider)
    fake_response = {
        "content": [{"type": "text", "text": '{"is_it": false}'}],
        "model": "claude-3-5-sonnet",
    }
    with patch("auction_tracker.ai.providers._post_json", return_value=fake_response):
        resp = client.complete("classify this")
    assert resp.json() == {"is_it": False}


# ---------------------------------------------------------------------------
# AIEngine — fallback, caching, budget, fail-open
# ---------------------------------------------------------------------------


def test_ai_engine_no_providers_returns_none(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config({"ai": {"providers": [], "enabled": True}})
    engine = AIEngine(cfg, store=None)
    assert engine.available_for("classify") is False
    assert engine.run("classify", "prompt") is None


def test_ai_engine_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {"enabled": False, "providers": [{
            "name": "openai", "kind": "openai", "base_url": "https://x",
            "model": "m", "api_key_env": "OPENAI_API_KEY",
        }]},
    })
    engine = AIEngine(cfg, store=None)
    assert engine.run("classify", "prompt") is None


def test_ai_engine_fail_open_returns_none_on_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {"fail_open": True, "providers": [{
            "name": "openai", "kind": "openai", "base_url": "https://x",
            "model": "m", "api_key_env": "OPENAI_API_KEY",
        }]},
    })
    engine = AIEngine(cfg, store=None)
    with patch("auction_tracker.ai.providers._post_json",
               side_effect=AIError("HTTP 500")):
        result = engine.run("classify", "prompt")
    assert result is None
    assert engine.failures == 1


def test_ai_engine_fail_closed_raises_on_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {"fail_open": False, "providers": [{
            "name": "openai", "kind": "openai", "base_url": "https://x",
            "model": "m", "api_key_env": "OPENAI_API_KEY",
        }]},
    })
    engine = AIEngine(cfg, store=None)
    with patch("auction_tracker.ai.providers._post_json",
               side_effect=AIError("HTTP 500")):
        with pytest.raises(AIError):
            engine.run("classify", "prompt")


def test_ai_engine_budget_exhausted_returns_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {"max_calls_per_cycle": 1, "providers": [{
            "name": "openai", "kind": "openai", "base_url": "https://x",
            "model": "m", "api_key_env": "OPENAI_API_KEY",
        }]},
    })
    engine = AIEngine(cfg, store=None)
    engine.calls_made = 1  # already at budget
    assert engine.budget_left == 0
    result = engine.run("classify", "prompt")
    assert result is None


def test_ai_engine_caches_in_store(monkeypatch, store):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = Config({
        "ai": {"cache_enabled": True, "cache_ttl_days": 30, "providers": [{
            "name": "openai", "kind": "openai", "base_url": "https://x",
            "model": "m", "api_key_env": "OPENAI_API_KEY",
        }]},
    })
    engine = AIEngine(cfg, store=store)
    fake = {"choices": [{"message": {"content": '{"is_it": true}'}}], "model": "m"}
    with patch("auction_tracker.ai.providers._post_json", return_value=fake) as mock_post:
        r1 = engine.run("classify", "prompt", auction_id=1)
        r2 = engine.run("classify", "prompt", auction_id=1)
    assert r1 is not None
    assert r2 is not None
    assert mock_post.call_count == 1, "second call should hit the cache"
    assert engine.cache_hits == 1


def test_ai_engine_fallback_to_second_provider(monkeypatch):
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
            "tasks": {"classify": {"providers": ["openai", "anthropic"]}},
        }
    })
    engine = AIEngine(cfg, store=None)
    fake_anthropic = {"content": [{"type": "text", "text": '{"is_it": true}'}], "model": "m"}
    call_count = {"n": 0}

    def fake_post(url, payload, headers, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise AIError("HTTP 500")
        return fake_anthropic

    with patch("auction_tracker.ai.providers._post_json", side_effect=fake_post):
        result = engine.run("classify", "prompt")
    assert result is not None
    assert result.provider == "anthropic"
    assert engine.failures == 1
    assert engine.calls_made == 1


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_build_comparable_terms_prefers_brand_model():
    terms = build_comparable_terms("HP ZBook G8 i7 16GB", brand="HP", model="ZBook G8")
    assert "HP ZBook G8" in terms
    assert "ZBook G8" in terms
    assert "HP" in terms


def test_build_comparable_terms_falls_back_to_words():
    terms = build_comparable_terms("assorted laptops with chargers")
    # "assorted" and "with" are stopwords; "laptops" and "chargers" survive.
    assert "laptops" in terms
    assert "chargers" in terms
    assert "assorted" not in terms
    assert "with" not in terms


def test_ai_engine_uses_store_providers_and_fallbacks(store):
    store.save_ai_providers([
        {
            "name": "primary-provider",
            "kind": "openai",
            "base_url": "https://p1",
            "model": "model-1",
            "api_key": "key1",
            "enabled": True,
            "tasks": ["classify"],
        },
        {
            "name": "fallback-provider",
            "kind": "openai",
            "base_url": "https://p2",
            "model": "model-2",
            "api_key": "key2",
            "enabled": True,
            "tasks": ["classify"],
        },
        {
            "name": "specs-only-provider",
            "kind": "openai",
            "base_url": "https://p3",
            "model": "model-3",
            "api_key": "key3",
            "enabled": True,
            "tasks": ["extract_specs"],
        },
    ])
    cfg = Config({"ai": {"enabled": True}})
    engine = AIEngine(cfg, store=store)

    chain = engine.providers("classify")
    assert len(chain) == 2
    assert [p.name for p in chain] == ["primary-provider", "fallback-provider"]

    call_count = {"n": 0}

    def fake_post(url, payload, headers, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise AIError("Primary provider error")
        return {"choices": [{"message": {"content": '{"ok": true}'}}], "model": "model-2"}

    with patch("auction_tracker.ai.providers._post_json", side_effect=fake_post):
        result = engine.run("classify", "prompt", use_cache=False)

    assert result is not None
    assert result.provider == "fallback-provider"
    assert engine.failures == 1
    assert engine.calls_made == 1

