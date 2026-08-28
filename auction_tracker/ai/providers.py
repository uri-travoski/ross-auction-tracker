"""Talking to AI providers.

Two wire protocols cover essentially every option an operator might want:

* ``openai``    — ``POST {base_url}/chat/completions``. Works with OpenAI,
  OpenRouter, Groq, Together, DeepSeek, Mistral, vLLM, LM Studio, llama.cpp
  and Ollama's OpenAI-compatible shim.
* ``anthropic`` — ``POST {base_url}/messages``.

Add a provider by appending to ``ai.providers`` in config.yaml and putting its
key in .env; no code change is needed unless the API shape is neither of the
above, in which case add a ``_call_<kind>`` method here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..config import AIProvider
from ..logging_setup import get_logger

log = get_logger(__name__)

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


class AIError(RuntimeError):
    """A provider call failed. The caller decides whether to try the next one."""


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    duration_ms: int = 0
    raw: dict[str, Any] | None = None

    def json(self, default: Any = None) -> Any:
        """Parse the reply as JSON, tolerating markdown fences and prose."""
        return extract_json(self.text, default)


def extract_json(text: str, default: Any = None) -> Any:
    """Best-effort JSON extraction from a model reply."""
    if not text:
        return default
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1] if "```" in candidate[3:] else candidate[3:]
        if candidate.lstrip().lower().startswith("json"):
            candidate = candidate.lstrip()[4:]
    candidate = candidate.strip().strip("`").strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    # Fall back to the outermost {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                continue
    return default


def _post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int
) -> dict[str, Any]:
    """POST JSON and return the parsed response, raising ``AIError`` on failure."""
    body = json.dumps(payload).encode("utf-8")
    if requests is not None:
        try:
            response = requests.post(
                url, data=body, headers=headers, timeout=timeout
            )
        except Exception as exc:
            raise AIError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise AIError(f"HTTP {response.status_code}: {response.text[:400]}")
        try:
            return response.json()
        except ValueError as exc:
            raise AIError(f"invalid JSON response: {response.text[:200]}") from exc

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400] if hasattr(exc, "read") else ""
        raise AIError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise AIError(f"{type(exc).__name__}: {exc}") from exc


class ProviderClient:
    """Stateless caller for one configured provider."""

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # -- public -----------------------------------------------------------
    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AIResponse:
        started = time.monotonic()
        kind = self.provider.kind
        if kind == "anthropic":
            text, raw = self._call_anthropic(
                prompt, system, max_tokens, temperature
            )
        elif kind in {"openai", "openai-compatible", "openrouter", "ollama"}:
            text, raw = self._call_openai(
                prompt, system, json_mode, max_tokens, temperature
            )
        else:
            raise AIError(f"unsupported provider kind {kind!r}")
        return AIResponse(
            text=text.strip(),
            provider=self.provider.name,
            model=self.provider.model,
            duration_ms=int((time.monotonic() - started) * 1000),
            raw=raw,
        )

    # -- protocols --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(self.provider.extra_headers)
        return headers

    def _call_openai(
        self,
        prompt: str,
        system: str,
        json_mode: bool,
        max_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, Any]]:
        p = self.provider
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "max_tokens": max_tokens or p.max_tokens,
            "temperature": p.temperature if temperature is None else temperature,
        }
        if json_mode:
            # Ignored by providers that do not support it.
            payload["response_format"] = {"type": "json_object"}
        headers = self._headers()
        if p.api_key:
            headers["Authorization"] = f"Bearer {p.api_key}"

        data = _post_json(
            f"{p.base_url}/chat/completions", payload, headers, p.timeout_seconds
        )
        if data.get("error"):
            raise AIError(str(data["error"])[:400])
        choices = data.get("choices") or []
        if not choices:
            raise AIError(f"no choices in response: {str(data)[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Some gateways return content as a list of parts.
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return str(content or ""), data

    def _call_anthropic(
        self,
        prompt: str,
        system: str,
        max_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, Any]]:
        p = self.provider
        payload: dict[str, Any] = {
            "model": p.model,
            "max_tokens": max_tokens or p.max_tokens,
            "temperature": p.temperature if temperature is None else temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        headers = self._headers()
        headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
        if p.api_key:
            headers["x-api-key"] = p.api_key

        data = _post_json(f"{p.base_url}/messages", payload, headers, p.timeout_seconds)
        if data.get("error"):
            raise AIError(str(data["error"])[:400])
        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") in (None, "text")
        )
        if not text:
            raise AIError(f"empty response: {str(data)[:200]}")
        return text, data


def describe_providers(providers: list[AIProvider]) -> list[dict[str, Any]]:
    """Summary for the status command and the web UI (never leaks keys)."""
    return [
        {
            "name": p.name,
            "kind": p.kind,
            "model": p.model,
            "base_url": p.base_url,
            "enabled": p.enabled,
            "key_env": p.api_key_env,
            "key_present": bool(p.api_key),
            "available": p.available,
        }
        for p in providers
    ]
