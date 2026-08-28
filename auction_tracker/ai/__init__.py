"""AI integration: pluggable providers plus the concrete analysis tasks.

The operator adds providers in ``config.yaml`` (``ai.providers``) and their keys
in ``.env``; ``ai.tasks`` then assigns providers to jobs in preference order.
See ``AGENTS.md`` for worked examples.
"""

from .providers import AIError, AIResponse, ProviderClient, describe_providers, extract_json
from .tasks import (
    ASK,
    CLASSIFY,
    ESTIMATE_PRICE,
    EXTRACT_SPECS,
    SUMMARIZE_SCAN,
    AIEngine,
    build_comparable_terms,
    context_for_question,
)

__all__ = [
    "AIEngine",
    "AIError",
    "AIResponse",
    "ProviderClient",
    "describe_providers",
    "extract_json",
    "build_comparable_terms",
    "context_for_question",
    "CLASSIFY",
    "EXTRACT_SPECS",
    "SUMMARIZE_SCAN",
    "ESTIMATE_PRICE",
    "ASK",
]
