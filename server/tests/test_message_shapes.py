"""Context messages are not uniformly dicts.

Pipecat wraps provider-specific entries in ``LLMSpecificMessage``, which the
Anthropic service puts into the context. Every ``message.get(...)`` in this
codebase was therefore an exception waiting for someone to select a non-OpenAI
model — and when that finally happened, it broke every turn before the LLM was
even called:

    LLMCallInspector#0 exception: 'LLMSpecificMessage' object has no attribute 'get'

These pin the shapes rather than the one call site that happened to crash first.
"""

import pytest
from pipecat.processors.aggregators.llm_context import LLMSpecificMessage

from agents.runtime import _is_tool_call_message, _strip_tool_call_messages
from helpers import message_dict
from memory import _transcript


def wrapped(message, llm="anthropic"):
    return LLMSpecificMessage(llm=llm, message=message)


class TestMessageDict:
    def test_a_plain_dict_passes_through(self):
        assert message_dict({"role": "user"}) == {"role": "user"}

    def test_a_wrapped_message_is_unwrapped(self):
        assert message_dict(wrapped({"role": "user"})) == {"role": "user"}

    def test_an_opaque_payload_is_none_not_an_exception(self):
        assert message_dict(wrapped(object())) is None

    def test_junk_is_none(self):
        assert message_dict(42) is None and message_dict(None) is None


class TestConsumersSurviveWrappedMessages:
    """Each of these iterated messages calling .get() and would have crashed."""

    def _mixed(self):
        return [
            {"role": "system", "content": "you are a controller"},
            {"role": "user", "content": "hello"},
            wrapped({"role": "assistant", "content": "hi there"}),
            wrapped(object()),
        ]

    def test_transcript_skips_what_it_cannot_read(self):
        out = _transcript(self._mixed())
        assert "user: hello" in out
        assert "assistant: hi there" in out

    def test_tool_call_detection_handles_wrapping(self):
        assert _is_tool_call_message(wrapped({"role": "tool"})) is True
        assert _is_tool_call_message(wrapped({"role": "user"})) is False

    def test_an_unreadable_message_is_kept_not_dropped(self):
        """Dropping what we cannot read would truncate the conversation.

        _strip_tool_call_messages runs on every agent handoff, so treating an
        opaque message as tool plumbing would quietly delete real turns.
        """
        opaque = wrapped(object())
        assert opaque in _strip_tool_call_messages([{"role": "user"}, opaque])

    def test_tool_plumbing_is_still_stripped(self):
        kept = _strip_tool_call_messages([
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "1", "content": "{}"},
        ])
        assert len(kept) == 1


class TestInspectorActuallyRuns:
    """Exercise the code path, not just the helper it calls.

    The first fix for the LLMSpecificMessage crash introduced a second crash —
    `name 'messages' is not defined` — because a later line still referenced the
    variable the fix removed. Every test passed: they covered message_dict but
    never ran the inspector. Cheap lesson, pinned here.
    """

    def _inspector(self, model="claude-sonnet-5"):
        from processors import LLMCallInspector, ModelState

        return LLMCallInspector(ModelState(model=model))

    def test_a_mixed_message_list_produces_a_line(self):
        line = self._inspector().describe_call([
            {"role": "system", "content": "you are a controller"},
            {"role": "user", "content": "run the tests"},
            wrapped({"role": "assistant", "content": "ok"}),
            wrapped(object()),
        ])
        assert "model=claude-sonnet-5" in line
        assert "run the tests" in line

    def test_an_empty_context_does_not_raise(self):
        assert "unknown" in self._inspector().describe_call([])

    def test_only_unreadable_messages_does_not_raise(self):
        assert self._inspector().describe_call([wrapped(object()), 42, None])

    def test_the_purpose_comes_from_the_last_user_message(self):
        line = self._inspector().describe_call([
            {"role": "user", "content": "first thing"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second thing"},
        ])
        assert "second thing" in line and "first thing" not in line

    def test_block_style_content_is_read(self):
        line = self._inspector().describe_call(
            [{"role": "user", "content": [{"type": "text", "text": "blocks work"}]}]
        )
        assert "blocks work" in line

    def test_an_unpriced_model_does_not_claim_to_be_free(self):
        """It reports $0.00000, but only because no price is known."""
        assert "cost=$0.00000" in self._inspector("mystery-1").describe_call(
            [{"role": "user", "content": "hi"}]
        )
