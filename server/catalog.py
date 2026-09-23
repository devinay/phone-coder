"""Model catalogue built from the providers' own APIs.

The dropdown used to be filled by a gateway that knew every model and its
capabilities. Without it the choice collapsed to eight hardcoded ids that went
stale the moment a provider shipped anything — and a model missing from that
list is not merely absent from the picker, it is *rejected*: switch_llm_model
returns MODEL_UNKNOWN and per-agent overrides silently do nothing.

So the catalogue is fetched instead. The two providers need different treatment,
and the difference is not incidental:

* **Anthropic** publishes capabilities. ``GET /v1/models`` returns context
  window, max output, and a capability tree per model, so the entry describes
  itself and needs no maintenance here.
* **OpenAI** publishes ids and nothing else. There is no field saying which
  models do tool calling, and the controller cannot work without it — an
  embedding, audio, or image model in the picker is a broken session waiting to
  happen. So ids are filtered against the families below. That list is the one
  thing here that has to be maintained by hand; it is the cost of an API that
  does not describe its own models.

Every network call is best-effort. A provider that cannot be reached
contributes nothing and the rest of the catalogue still builds, because losing
the picker is a worse failure than showing a short list.
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from loguru import logger

_TIMEOUT = 10.0

# OpenAI families that serve chat completions and can take tool definitions.
# Deliberately an allow-list: a wrong inclusion puts a model in the picker that
# fails at the first tool call, which is far more confusing than an omission.
_OPENAI_ALLOWED = re.compile(r"^(gpt-4|gpt-5|gpt-6|o1|o3|o4|chat-latest)", re.I)

# Anything matching these is not a chat model, or cannot drive the controller.
# `codex` and `search-api` are excluded because they are served through other
# APIs than the Chat Completions client this pipeline uses.
_OPENAI_DENY = re.compile(
    r"audio|realtime|image|sora|embed|tts|whisper|transcribe|moderation"
    r"|search-api|search-preview|codex|instruct|dall|deep-research",
    re.I,
)

# A dated snapshot of a model that is also offered unsuffixed, e.g.
# `gpt-5.4-mini-2026-03-17` beside `gpt-5.4-mini`. Dropped when the base exists,
# purely so the picker stays readable.
# Covers both `-YYYY-MM-DD` and OpenAI's older bare `-MMDD` snapshot suffix.
_DATED = re.compile(r"^(?P<base>.+?)-(?:\d{4}-\d{2}-\d{2}|\d{4})$")


@dataclass
class ModelInfo:
    """One selectable model, however the provider described it."""

    id: str
    provider: str            # which service drives it: openai | anthropic | ollama
    display_name: str = ""
    context: int | None = None
    max_output: int | None = None
    supports_tools: bool = True
    capabilities: dict = field(default_factory=dict)

    @property
    def vendor(self) -> str:
        """Upstream vendor. Decides whether a switch can preserve context."""
        return self.provider

    def describe(self) -> str:
        bits = [self.display_name or self.id]
        if self.context:
            bits.append(f"{self.context // 1000}K ctx")
        return " · ".join(bits)


def _get_json(url: str, headers: dict) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"[CATALOG] {url} -> HTTP {e.code}; skipping this provider")
    except Exception as e:
        logger.warning(f"[CATALOG] {url} unreachable ({type(e).__name__}); skipping")
    return None


def fetch_anthropic(api_key: str) -> list[ModelInfo]:
    """Anthropic models, with the capabilities the API reports."""
    if not api_key:
        return []
    data = _get_json(
        "https://api.anthropic.com/v1/models?limit=100",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    if not data:
        return []
    models = []
    for m in data.get("data", []):
        caps = m.get("capabilities") or {}
        models.append(
            ModelInfo(
                id=m["id"],
                provider="anthropic",
                display_name=m.get("display_name", ""),
                context=m.get("max_input_tokens"),
                max_output=m.get("max_tokens"),
                # Every current Claude model takes tools; the capability tree
                # does not carry a tool-use flag, so this is not inferred from
                # absent data.
                supports_tools=True,
                capabilities={k: v for k, v in caps.items() if isinstance(v, dict)},
            )
        )
    logger.info(f"[CATALOG] anthropic: {len(models)} models")
    return models


def openai_is_selectable(model_id: str) -> bool:
    """Whether an OpenAI id looks like a chat model that can take tools."""
    return bool(_OPENAI_ALLOWED.match(model_id)) and not _OPENAI_DENY.search(model_id)


def drop_dated_duplicates(ids: list[str]) -> list[str]:
    """Remove `x-YYYY-MM-DD` when plain `x` is also offered."""
    have = set(ids)
    keep = []
    for i in ids:
        m = _DATED.match(i)
        if m and m.group("base") in have:
            continue
        keep.append(i)
    return keep


def fetch_openai(api_key: str) -> list[ModelInfo]:
    """OpenAI models, filtered to the families that can drive an agent.

    The API returns ids only — no context window, no capability flags — so
    everything beyond the id is unknown here and left unset rather than guessed.
    """
    if not api_key:
        return []
    data = _get_json("https://api.openai.com/v1/models", {"Authorization": f"Bearer {api_key}"})
    if not data:
        return []
    ids = sorted(m["id"] for m in data.get("data", []))
    selectable = drop_dated_duplicates([i for i in ids if openai_is_selectable(i)])
    logger.info(f"[CATALOG] openai: {len(selectable)} selectable of {len(ids)} returned")
    return [ModelInfo(id=i, provider="openai") for i in selectable]


def build_catalog(
    anthropic_key: str = "",
    openai_key: str = "",
    extra: list[ModelInfo] | None = None,
) -> dict[str, ModelInfo]:
    """Every selectable model, keyed by id. Never raises."""
    models: list[ModelInfo] = []
    models += fetch_anthropic(anthropic_key)
    models += fetch_openai(openai_key)
    models += extra or []
    catalog = {m.id: m for m in models}
    logger.info(f"[CATALOG] {len(catalog)} models selectable in total")
    return catalog


def grouped(catalog: dict[str, ModelInfo]) -> dict[str, list[str]]:
    """Ids grouped by provider, for the cockpit's optgroups."""
    out: dict[str, list[str]] = {}
    for info in catalog.values():
        out.setdefault(info.provider, []).append(info.id)
    return {k: sorted(v) for k, v in sorted(out.items())}
