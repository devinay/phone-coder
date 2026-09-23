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
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_TIMEOUT = 10.0

# Providers this module knows how to query, and the env var that unlocks each.
# Discovery is by key presence, so adding a key to .env is all it takes to bring
# a provider into the picker — nothing here needs editing for that.
PROVIDER_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def configured_providers(env: dict[str, str] | None = None) -> list[str]:
    """Providers whose key is actually present, in declaration order."""
    src = env if env is not None else os.environ
    return [p for p, var in PROVIDER_KEYS.items() if (src.get(var) or "").strip()]

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


# Per-million-token prices, (input, output) in USD. Neither provider exposes
# pricing through its API — the model endpoints return no price field — so this
# is transcribed from the published pricing pages and has to be refreshed by
# hand when they change.
#
# Fetched 2026-09-23 from:
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/pricing
#
# A model absent from this table is priced None and displayed as unknown rather
# than as free: a wrong zero reads as "this costs nothing", which is the one
# mistake a cost display must not make.
_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5-20251101": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # OpenAI
    "gpt-6-astra": (10.0, 50.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.50, 15.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.2": (1.75, 14.0),
    "gpt-5.1": (1.25, 10.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "o1": (15.0, 60.0),
    "o3": (2.0, 8.0),
    "o4-mini": (1.10, 4.40),
    # Local: no marginal cost, and a genuine zero rather than a missing one.
    "qwen2.5-coder:7b": (0.0, 0.0),
}


# The picker defaults to these rather than everything the account can reach.
# Fetching found 48 selectable models, which is a scroll, not a choice.
#
# Ordered best-first and chosen to span a range rather than to rank: a flagship,
# a strong general-purpose model, a value workhorse, and something cheap for
# high-volume work. Where two models are close, the cheaper and newer one wins —
# which is why Opus 5 is listed below Opus 5.5 and Sonnet 4.6 is absent entirely,
# both being superseded by something better *and* cheaper.
#
# Honest limit: this is ordered by tier, price and release, not by measured
# capability. Several of these post-date anything I can speak to first-hand.
# Set MODEL_SHORTLIST=off to get the full fetched catalogue back.
_SHORTLIST: dict[str, list[str]] = {
    "anthropic": [
        "claude-fable-5-1",           # $10/$50 — most capable
        "claude-opus-5-5",            # $4/$20  — best general, undercuts Opus 5
        "claude-sonnet-5",            # $2/$10  — best value
        "claude-haiku-4-5-20251001",  # $1/$5   — fast and cheap
        "claude-opus-5",              # $5/$25  — previous Opus, stable fallback
    ],
    "openai": [
        "gpt-6-astra",   # $10/$50   — flagship
        "gpt-6-sol",     # $2/$10    — mid tier, newest line
        "gpt-5.5",       # $5/$30    — previous flagship
        "gpt-5.4-mini",  # $0.75/$4.5 — value
        "gpt-6-luna",    # $0.10/$0.50 — cheapest
    ],
}


# Written by the refresh-models skill. Holds the things no API publishes —
# price, vision support on providers that do not declare it, a one-line ability
# blurb, and the ranking — so the runtime never has to scrape anything on boot.
CURATED_PATH = Path(__file__).parent / "model_shortlist.json"


def load_curated(path: Path | None = None) -> dict:
    """The skill's output, or an empty result when it has never been run."""
    p = path or CURATED_PATH
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        logger.info("[CATALOG] no model_shortlist.json; using the built-in shortlist")
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[CATALOG] model_shortlist.json unreadable ({e}); ignoring it")
        return {}
    logger.info(
        f"[CATALOG] curated shortlist from {data.get('generated', 'an unknown date')}: "
        f"{sum(len(v) for v in data.get('providers', {}).values())} models"
    )
    return data


def shortlisted(models: list["ModelInfo"], keep: set[str]) -> list["ModelInfo"]:
    """Narrow to the curated list, never dropping a model already in use.

    ``keep`` carries the configured models — the default and any per-agent
    override. Excluding one of those would not merely hide it: an id outside the
    catalogue is *rejected* by switch_llm_model, so a shortlist that dropped the
    running model would silently break the thing it was meant to tidy.
    """
    wanted = {m for ids in _SHORTLIST.values() for m in ids} | keep
    chosen = [m for m in models if m.id in wanted]
    # Curated order first, then anything kept only because it is in use. Rank is
    # per provider, since the picker groups by provider anyway.
    rank = {mid: i for ids in _SHORTLIST.values() for i, mid in enumerate(ids)}
    return sorted(chosen, key=lambda m: (rank.get(m.id, len(rank)), m.id))


def price_of(model_id: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None when not published here."""
    return _PRICES.get(model_id)


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
    price: tuple[float, float] | None = None
    # From the curated file. `vision` is tri-state on purpose: True and False
    # are claims, None means nobody has said — and for OpenAI nobody does,
    # so guessing here would be inventing a capability.
    vision: bool | None = None
    blurb: str = ""

    @property
    def vendor(self) -> str:
        """Upstream vendor. Decides whether a switch can preserve context."""
        return self.provider

    def describe(self) -> str:
        bits = [self.display_name or self.id]
        if self.context:
            bits.append(f"{self.context // 1000}K ctx")
        bits.append(self.price_label)
        return " · ".join(b for b in bits if b)

    @property
    def vision_label(self) -> str:
        """Vision support as a claim, or an explicit absence of one."""
        return {True: "vision", False: "no vision", None: "vision unknown"}[self.vision]

    @property
    def price_label(self) -> str:
        """Short price for the picker, or an explicit unknown.

        Never renders an absent price as $0 — that reads as free, which is the
        one thing a cost display must not get wrong.
        """
        if self.price is None:
            return "price unknown"
        inp, out = self.price
        if (inp, out) == (0.0, 0.0):
            return "local, free"
        return f"${inp:g}/${out:g} per Mtok"


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
                price=price_of(m["id"]),
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
    return [ModelInfo(id=i, provider="openai", price=price_of(i)) for i in selectable]


def apply_curated(models: list[ModelInfo], curated: dict) -> list[ModelInfo]:
    """Overlay the skill's findings onto what the providers reported.

    The provider is the authority on what exists and what it can do; the curated
    file is the authority on price, vision where unpublished, and the blurb. So
    the overlay only fills fields the API left empty — a stale curated file can
    never contradict a live capability.
    """
    entries = {
        m["id"]: m
        for ids in curated.get("providers", {}).values()
        for m in ids
    }
    for info in models:
        e = entries.get(info.id)
        if not e:
            continue
        if info.price is None and e.get("price"):
            info.price = tuple(e["price"])
        if info.vision is None and e.get("vision") is not None:
            info.vision = e["vision"]
        info.blurb = info.blurb or e.get("blurb", "")
    return models


def curated_shortlist(models: list[ModelInfo], curated: dict, keep: set[str]) -> list[ModelInfo]:
    """Narrow using the skill's ranking instead of the built-in list."""
    order = {
        m["id"]: i
        for ids in curated.get("providers", {}).values()
        for i, m in enumerate(ids)
    }
    wanted = set(order) | keep
    chosen = [m for m in models if m.id in wanted]
    return sorted(chosen, key=lambda m: (order.get(m.id, len(order)), m.id))


def build_catalog(
    anthropic_key: str = "",
    openai_key: str = "",
    extra: list[ModelInfo] | None = None,
    shortlist: bool = True,
    in_use: set[str] | None = None,
    curated: dict | None = None,
) -> dict[str, ModelInfo]:
    """Every selectable model, keyed by id. Never raises."""
    models: list[ModelInfo] = []
    models += fetch_anthropic(anthropic_key)
    models += fetch_openai(openai_key)
    curated = load_curated() if curated is None else curated
    if shortlist:
        before = len(models)
        # The skill's ranking wins when it exists; the built-in list is the
        # fallback for a checkout where the skill has never been run.
        models = (
            curated_shortlist(models, curated, in_use or set())
            if curated
            else shortlisted(models, in_use or set())
        )
        logger.info(f"[CATALOG] shortlisted {len(models)} of {before} fetched models")
    # Local models are added after the shortlist: there is only ever one, and it
    # is present because the user installed it, which is choice enough.
    models += extra or []
    # Applied last, so it reaches the extras too — the local model needs its
    # vision flag as much as any fetched one, and more urgently: a coder model
    # offered for sketching is a guaranteed failure.
    if curated:
        models = apply_curated(models, curated)
    catalog = {m.id: m for m in models}
    logger.info(f"[CATALOG] {len(catalog)} models selectable in total")
    return catalog


def grouped(catalog: dict[str, ModelInfo]) -> dict[str, list[str]]:
    """Ids grouped by provider, for the cockpit's optgroups.

    Insertion order is preserved rather than sorted, because the catalogue is
    already ordered deliberately — best-first within each provider — and sorting
    would throw that away in favour of alphabetical, which puts Haiku above Opus.
    """
    out: dict[str, list[str]] = {}
    for info in catalog.values():
        out.setdefault(info.provider, []).append(info.id)
    return dict(sorted(out.items()))
