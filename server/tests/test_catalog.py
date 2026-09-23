"""Tests for the model catalogue.

The filtering is the part worth pinning down. Anthropic describes its own
models, so there is nothing to judge; OpenAI returns bare ids, and deciding
which of them can drive an agent is a maintained guess that will drift.
"""

import catalog
from catalog import (
    ModelInfo,
    build_catalog,
    drop_dated_duplicates,
    grouped,
    openai_is_selectable,
)

# Real ids, taken from a live GET /v1/models on a working account.
REAL_OPENAI_IDS = [
    "chat-latest", "gpt-3.5-turbo", "gpt-4", "gpt-4-0613", "gpt-4o", "gpt-4o-mini",
    "gpt-4o-mini-tts", "gpt-4o-transcribe", "gpt-5", "gpt-5-codex", "gpt-5-search-api",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-mini-2026-03-17", "gpt-5.5-pro", "gpt-6-astra",
    "gpt-audio", "gpt-image-2", "gpt-realtime", "o1", "o1-pro", "o3",
    "o4-mini-deep-research", "omni-moderation-latest", "sora-2",
    "text-embedding-3-large", "tts-1-hd", "whisper-1",
]


class TestOpenAIFiltering:
    """A wrong inclusion is worse than an omission.

    An embedding or audio model in the picker looks selectable and then fails at
    the first tool call, which is far more confusing than simply not being there.
    """

    def test_chat_families_are_kept(self):
        for i in ["gpt-4o", "gpt-4.1", "gpt-5", "gpt-5.4-mini", "gpt-6-astra", "o3", "o1-pro"]:
            assert openai_is_selectable(i), i

    def test_non_chat_modalities_are_excluded(self):
        for i in [
            "gpt-image-2", "sora-2", "whisper-1", "tts-1-hd", "gpt-audio",
            "gpt-realtime", "text-embedding-3-large", "omni-moderation-latest",
        ]:
            assert not openai_is_selectable(i), i

    def test_models_served_by_other_apis_are_excluded(self):
        """Codex, search and deep-research are not Chat Completions + tools."""
        for i in ["gpt-5-codex", "gpt-5-search-api", "o4-mini-deep-research",
                  "gpt-4o-mini-search-preview", "gpt-3.5-turbo-instruct"]:
            assert not openai_is_selectable(i), i

    def test_legacy_completion_models_are_excluded(self):
        for i in ["gpt-3.5-turbo", "davinci-002", "babbage-002"]:
            assert not openai_is_selectable(i), i

    def test_the_whole_real_list_filters_sanely(self):
        kept = [i for i in REAL_OPENAI_IDS if openai_is_selectable(i)]
        assert "gpt-5.4-mini" in kept
        assert "gpt-image-2" not in kept
        # Roughly a third of a real account's catalogue is chat-capable.
        assert 0 < len(kept) < len(REAL_OPENAI_IDS)


class TestDatedDuplicates:
    def test_dated_snapshot_is_dropped_when_the_base_exists(self):
        assert drop_dated_duplicates(
            ["gpt-5.4-mini", "gpt-5.4-mini-2026-03-17"]
        ) == ["gpt-5.4-mini"]

    def test_short_snapshot_suffix_is_dropped_too(self):
        """OpenAI's older `-MMDD` form, e.g. gpt-4-0613 beside gpt-4."""
        assert drop_dated_duplicates(["gpt-4", "gpt-4-0613"]) == ["gpt-4"]

    def test_a_dated_id_with_no_base_survives(self):
        """Anthropic ships some ids only in dated form; dropping them loses them."""
        assert drop_dated_duplicates(
            ["claude-haiku-4-5-20251001"]
        ) == ["claude-haiku-4-5-20251001"]

    def test_a_suffix_that_is_not_a_date_survives(self):
        assert "o1-pro" in drop_dated_duplicates(["o1", "o1-pro"])


class TestCatalogAssembly:
    def test_an_unreachable_provider_contributes_nothing(self, monkeypatch):
        """Losing the picker is a worse failure than a short list."""
        monkeypatch.setattr(catalog, "_get_json", lambda *a, **k: None)
        assert build_catalog("key", "key") == {}

    def test_no_key_means_no_call(self, monkeypatch):
        called = []
        monkeypatch.setattr(catalog, "_get_json", lambda *a, **k: called.append(a) or None)
        build_catalog("", "")
        assert called == []

    def test_extras_are_merged(self, monkeypatch):
        monkeypatch.setattr(catalog, "_get_json", lambda *a, **k: None)
        cat = build_catalog("", "", extra=[ModelInfo(id="qwen2.5-coder:7b", provider="ollama")])
        assert cat["qwen2.5-coder:7b"].provider == "ollama"

    def test_anthropic_entries_carry_their_capabilities(self, monkeypatch):
        monkeypatch.setattr(catalog, "_get_json", lambda *a, **k: {
            "data": [{
                "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                "max_input_tokens": 1000000, "max_tokens": 128000,
                "capabilities": {"effort": {"supported": True}},
            }]
        })
        info = catalog.fetch_anthropic("key")[0]
        assert info.context == 1000000
        assert info.provider == "anthropic"
        assert "effort" in info.capabilities
        assert "1000K ctx" in info.describe()

    def test_grouping_is_by_provider(self):
        cat = {
            "a": ModelInfo(id="a", provider="anthropic"),
            "b": ModelInfo(id="b", provider="openai"),
            "c": ModelInfo(id="c", provider="anthropic"),
        }
        assert grouped(cat) == {"anthropic": ["a", "c"], "openai": ["b"]}


class TestPricing:
    """An unpriced model must never read as free."""

    def test_known_prices_are_labelled(self):
        assert ModelInfo(
            id="claude-opus-5-5", provider="anthropic", price=catalog.price_of("claude-opus-5-5")
        ).price_label == "$4/$20 per Mtok"

    def test_an_unpriced_model_says_so_rather_than_showing_zero(self):
        """A wrong $0 reads as 'this costs nothing' — the one unacceptable error."""
        info = ModelInfo(id="gpt-5.5-pro", provider="openai", price=catalog.price_of("gpt-5.5-pro"))
        assert info.price is None
        assert info.price_label == "price unknown"
        assert "$0" not in info.price_label

    def test_a_local_model_is_free_not_unknown(self):
        info = ModelInfo(
            id="qwen2.5-coder:7b", provider="ollama", price=catalog.price_of("qwen2.5-coder:7b")
        )
        assert info.price_label == "local, free"

    def test_prices_are_attached_when_models_are_fetched(self, monkeypatch):
        monkeypatch.setattr(catalog, "_get_json", lambda *a, **k: {
            "data": [{"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
                      "max_input_tokens": 1000000, "max_tokens": 128000, "capabilities": {}}]
        })
        assert catalog.fetch_anthropic("key")[0].price == (2.0, 10.0)

    def test_the_cheaper_newer_models_are_priced_correctly(self):
        """Opus 5.5 undercuts Opus 5, and Sonnet 5 undercuts Sonnet 4.6.

        Both are counterintuitive — newer and cheaper — so they are pinned here
        to catch a careless "newer must cost more" edit.
        """
        assert catalog.price_of("claude-opus-5-5") < catalog.price_of("claude-opus-5")
        assert catalog.price_of("claude-sonnet-5") < catalog.price_of("claude-sonnet-4-6")


class TestShortlist:
    """48 models is a scroll, not a choice — but narrowing must not strand one."""

    def _fetched(self):
        return [
            ModelInfo(id=i, provider="anthropic")
            for i in ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5",
                      "claude-haiku-4-5-20251001", "claude-opus-5",
                      "claude-sonnet-4-6", "claude-opus-4-6"]
        ] + [
            ModelInfo(id=i, provider="openai")
            for i in ["gpt-6-astra", "gpt-6-sol", "gpt-5.5", "gpt-5.4-mini",
                      "gpt-6-luna", "gpt-4o-mini", "o3"]
        ]

    def test_it_narrows_to_five_per_provider(self):
        kept = catalog.shortlisted(self._fetched(), keep=set())
        by_provider = {}
        for m in kept:
            by_provider.setdefault(m.provider, []).append(m.id)
        assert len(by_provider["anthropic"]) == 5
        assert len(by_provider["openai"]) == 5

    def test_superseded_models_are_dropped(self):
        """Sonnet 4.6 and Opus 4.6 are beaten on both capability and price."""
        kept = {m.id for m in catalog.shortlisted(self._fetched(), keep=set())}
        assert "claude-sonnet-4-6" not in kept
        assert "claude-opus-4-6" not in kept

    def test_a_configured_model_is_never_dropped(self):
        """Hiding the running model would break it, not tidy it.

        An id outside the catalogue is rejected by switch_llm_model, so a
        shortlist that excluded the configured model would be worse than none.
        """
        kept = {m.id for m in catalog.shortlisted(self._fetched(), keep={"claude-sonnet-4-6"})}
        assert "claude-sonnet-4-6" in kept

    def test_curated_order_is_preserved(self):
        """Alphabetical would put Haiku above Opus, which is backwards."""
        kept = [m.id for m in catalog.shortlisted(self._fetched(), keep=set())
                if m.provider == "anthropic"]
        assert kept[0] == "claude-fable-5-1"
        assert kept.index("claude-opus-5-5") < kept.index("claude-haiku-4-5-20251001")

    def test_the_full_catalogue_is_still_reachable(self, monkeypatch):
        monkeypatch.setattr(catalog, "fetch_anthropic", lambda k: self._fetched()[:7])
        monkeypatch.setattr(catalog, "fetch_openai", lambda k: self._fetched()[7:])
        assert len(build_catalog("k", "k", shortlist=False)) == 14
        assert len(build_catalog("k", "k", shortlist=True)) == 10

    def test_local_models_bypass_the_shortlist(self, monkeypatch):
        """There is only ever one, and installing it was choice enough."""
        monkeypatch.setattr(catalog, "fetch_anthropic", lambda k: [])
        monkeypatch.setattr(catalog, "fetch_openai", lambda k: [])
        cat = build_catalog("", "", extra=[ModelInfo(id="qwen2.5-coder:7b", provider="ollama")])
        assert "qwen2.5-coder:7b" in cat
