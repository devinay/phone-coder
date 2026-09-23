"""Tests for the system-prompt blocks several processors maintain.

Facts an agent needs must be present in the prompt, not remembered — tool
results are stripped on every agent handoff. That makes the blocks load-bearing,
and makes a block that silently disappears or goes stale a real failure.
"""

import pytest

from processors import ActiveModelInjector, ModelState, set_prompt_block

A = "\n\n[A — refreshed]\n"
B = "\n\n[B — refreshed]\n"


class TestBlockReplacement:
    def test_a_block_is_added_when_absent(self):
        assert set_prompt_block("base", A, "aaa") == "base\n\n[A — refreshed]\naaa"

    def test_rewriting_one_block_leaves_the_other(self):
        """The bug this exists to prevent.

        Splitting on your own marker and appending truncates every block after
        yours, so whichever processor ran last won and the rest vanished.
        """
        c = set_prompt_block(set_prompt_block("base", A, "aaa"), B, "bbb")
        c = set_prompt_block(c, A, "AAA")
        assert "AAA" in c and "bbb" in c and "aaa" not in c

    def test_order_does_not_matter(self):
        first = set_prompt_block(set_prompt_block("base", A, "x"), B, "y")
        second = set_prompt_block(set_prompt_block("base", B, "y"), A, "x")
        assert set(first.split("\n\n")) == set(second.split("\n\n"))

    def test_blocks_do_not_accumulate(self):
        c = "base"
        for _ in range(5):
            c = set_prompt_block(c, A, "same")
        assert c.count("[A — refreshed]") == 1


class TestActiveModelInjector:
    """Asked which model it runs, a model with no data guesses confidently."""

    def _state(self, model="claude-sonnet-5"):
        return ModelState(model=model, providers={"claude-sonnet-5": "anthropic"})

    def _catalog(self):
        from catalog import ModelInfo
        return {
            "claude-sonnet-5": ModelInfo(
                id="claude-sonnet-5", provider="anthropic", price=(2.0, 10.0)
            ),
            "claude-opus-5-5": ModelInfo(
                id="claude-opus-5-5", provider="anthropic", price=(4.0, 20.0)
            ),
        }

    def test_the_conversation_model_is_named(self):
        out = ActiveModelInjector(self._state(), None, catalog=self._catalog()).describe()
        assert "conversation: claude-sonnet-5" in out

    def test_provider_and_price_are_included(self):
        """So "is this the expensive one?" is answerable without a lookup."""
        out = ActiveModelInjector(self._state(), None, catalog=self._catalog()).describe()
        assert "anthropic" in out and "$2/$10" in out

    def test_vision_is_absent_until_it_is_wired(self):
        out = ActiveModelInjector(self._state(), None, vision=None).describe()
        assert "vision" not in out

    def test_vision_is_named_with_where_it_came_from(self):
        out = ActiveModelInjector(
            self._state(), None,
            vision=lambda: ("claude-opus-5-5", "set by voice this session"),
            catalog=self._catalog(),
        ).describe()
        assert "vision: claude-opus-5-5" in out
        assert "set by voice this session" in out

    def test_it_reads_the_live_state_rather_than_a_snapshot(self):
        """A block that drifted from what is running would be believed."""
        state = self._state()
        inj = ActiveModelInjector(state, None, catalog=self._catalog())
        state.model = "claude-opus-5-5"
        assert "claude-opus-5-5" in inj.describe()

    def test_an_unknown_model_is_still_named(self):
        """Missing catalogue data must not hide which model is running."""
        out = ActiveModelInjector(ModelState(model="mystery-1"), None, catalog={}).describe()
        assert "conversation: mystery-1" in out

    def test_a_failure_to_describe_does_not_break_the_turn(self):
        class Boom:
            @property
            def model(self):
                raise RuntimeError("no")

        inj = ActiveModelInjector(Boom(), None)
        with pytest.raises(RuntimeError):
            inj.describe()          # describe() itself propagates
        inj._refresh()              # but the turn-level path swallows it
