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
