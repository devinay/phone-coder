"""Context messages are not uniformly dicts.

Pipecat wraps provider-specific entries in ``LLMSpecificMessage``, which the
Anthropic service puts into the context. Every ``message.get(...)`` in this
codebase was therefore an exception waiting for someone to select a non-OpenAI
model — and when that finally happened, it broke every turn before the LLM was
even called:

    LLMCallInspector#0 exception: 'LLMSpecificMessage' object has no attribute 'get'

These pin the shapes rather than the one call site that happened to crash first.
"""

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
