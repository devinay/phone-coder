"""Grove gateway support for the controller LLM.

Grove is MongoDB's internal OpenAI-compatible gateway fronting models from many
vendors. This module wraps ``grove_python`` — the official client — for
configuration, catalogue discovery and capability lookup. The actual inference
calls still go through Pipecat's ``OpenAILLMService`` pointed at Grove's base
URL, so the pipeline keeps its streaming and tool-calling behaviour.

Everything here is inert unless ``GROVE_ENABLED=true``; with the flag off the
bot builds exactly the services it built before Grove existed.
"""

import os

from grove_python import (
    BASE_URL,
    MODEL_REGISTRY,
    RESPONSES_API_MODELS,
    GroveClient,
    get_model_info,
)
from loguru import logger

GROVE_ENABLED = os.getenv("GROVE_ENABLED", "false").lower() == "true"
GROVE_API_KEY = os.getenv("GROVE_API_KEY", "")
GROVE_BASE_URL = os.getenv("GROVE_BASE_URL", BASE_URL)
GROVE_DEFAULT_MODEL = os.getenv("GROVE_DEFAULT_MODEL", "gpt-5.4-mini")

# Ordered longest-prefix-first; the first match wins. grove_python's registry
# carries capabilities but not the upstream vendor, which is what decides
# whether a model switch can preserve context.
_VENDOR_PREFIXES: list[tuple[str, str]] = [
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("claude-", "anthropic"),
    ("llama-", "meta"),
    ("mistral-", "mistral"),
    ("cohere-", "cohere"),
    ("phi-", "microsoft"),
    ("deepseek-", "deepseek"),
    ("grok-", "xai"),
    ("kimi-", "moonshot"),
    ("embed-", "cohere"),
    ("fw-kimi", "moonshot"),
    ("fw-glm", "zhipu"),
    ("fw-deepseek", "deepseek"),
    ("fw-", "fireworks"),
]


def vendor_family(model_id: str) -> str:
    """Return the upstream vendor behind a Grove model id.

    Switching between two Grove models is only context-safe when this value is
    unchanged; different vendors disagree on tool-call and message encoding.
    """
    lowered = model_id.lower()
    for prefix, family in _VENDOR_PREFIXES:
        if lowered.startswith(prefix):
            return family
    return "unknown"


def supports_tools(model_id: str) -> bool:
    """Whether the model can take tool definitions.

    The controller registers every guarded tool on the LLM service, so a model
    without tool support cannot drive it.
    """
    info = get_model_info(model_id)
    return info.supports_tools if info else True


def uses_responses_api(model_id: str) -> bool:
    """Whether the model is served by OpenAI's Responses API.

    Pipecat's OpenAILLMService speaks Chat Completions, so these are surfaced
    in the picker but will fail if selected — flagged rather than hidden.
    """
    return model_id in RESPONSES_API_MODELS


def fetch_catalog(timeout: float = 10.0) -> list[str]:
    """Fetch selectable model ids from Grove, falling back to the bundled registry.

    Called once at startup. Availability is key-dependent, so the live call is
    the only authoritative source of what the running key can actually use;
    ``MODEL_REGISTRY`` keeps the app usable when the key is missing or pending
    approval.
    """
    if not GROVE_API_KEY:
        logger.warning("[GROVE] no GROVE_API_KEY set; using bundled grove_python registry")
        return sorted(MODEL_REGISTRY)
    try:
        client = GroveClient(api_key=GROVE_API_KEY, base_url=GROVE_BASE_URL, timeout=timeout)
        try:
            models = [m.id for m in client.list_models().data if m.id]
        finally:
            client.close()
        if not models:
            raise ValueError("empty model list")
        logger.info(f"[GROVE] catalogue fetched: {len(models)} models")
        return sorted(models)
    except Exception as e:
        logger.warning(f"[GROVE] list_models failed ({e}); using bundled grove_python registry")
        return sorted(MODEL_REGISTRY)


def grouped_catalog(models: list[str]) -> dict[str, list[str]]:
    """Group model ids by vendor family, for the cockpit dropdown's optgroups."""
    grouped: dict[str, list[str]] = {}
    for model in models:
        grouped.setdefault(vendor_family(model), []).append(model)
    return {family: sorted(ids) for family, ids in sorted(grouped.items())}
