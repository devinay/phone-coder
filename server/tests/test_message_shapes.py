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


class TestRenderability:
    """A message the provider cannot render kills the turn inside its adapter.

    Reproduced directly against pipecat's Anthropic adapter: a thought without a
    signature misses the adapter's thought branch, is passed through verbatim,
    and then `message["role"]` raises KeyError('role'). A model's own output can
    therefore poison its next turn.
    """

    def test_a_signature_less_thought_is_not_renderable(self):
        from helpers import is_renderable_message

        assert not is_renderable_message(wrapped({"type": "thought", "text": "hmm"}))

    def test_a_complete_thought_is_left_alone(self):
        from helpers import is_renderable_message

        assert is_renderable_message(
            wrapped({"type": "thought", "text": "hmm", "signature": "sig"})
        )

    def test_anything_with_a_role_is_renderable(self):
        from helpers import is_renderable_message

        assert is_renderable_message({"role": "user", "content": "hi"})
        assert is_renderable_message(wrapped({"role": "assistant", "content": "hi"}))

    def test_something_merely_unfamiliar_is_not_dropped(self):
        """Narrow on purpose — silently shortening a conversation is its own bug."""
        from helpers import is_renderable_message

        assert is_renderable_message(wrapped(object()))

    def test_the_adapter_really_does_fail_on_it(self):
        """Pins the upstream behaviour this guard exists for."""
        import pytest
        from pipecat.adapters.services.anthropic_adapter import AnthropicLLMAdapter

        with pytest.raises(KeyError):
            AnthropicLLMAdapter()._from_universal_context_messages([
                {"role": "user", "content": "hello"},
                wrapped({"type": "thought", "text": "hmm"}),
            ])

    def test_the_adapter_succeeds_once_the_guard_has_run(self):
        from pipecat.adapters.services.anthropic_adapter import AnthropicLLMAdapter

        from helpers import is_renderable_message

        raw = [
            {"role": "user", "content": "hello"},
            wrapped({"type": "thought", "text": "hmm"}),
            wrapped({"role": "assistant", "content": "hi"}),
        ]
        clean = [m for m in raw if is_renderable_message(m)]
        converted = AnthropicLLMAdapter()._from_universal_context_messages(clean)
        assert converted.messages


class TestSanitiserCoversTheToolPath:
    """A tool result reaches the LLM without a new user turn.

    The first sanitiser gated on TranscriptionFrame/LLMRunFrame, which left the
    entire tool-calling path unguarded — and that is exactly where the failure
    lives, because a thought and a tool call arrive in the same assistant turn:

        Calling function [list_doc_projects]
        LLMAssistantAggregator: Pushing context frame!
        AnthropicLLMService exception: 'role'
    """

    class _Ctx:
        def __init__(self, messages):
            self.messages = list(messages)

        def set_messages(self, messages):
            self.messages = list(messages)

    def _sanitiser(self, ctx):
        from processors import ContextSanitiser

        return ContextSanitiser(ctx)

    def test_an_unrenderable_message_is_removed_from_a_context_frame(self):
        ctx = self._Ctx([
            {"role": "user", "content": "start a note"},
            wrapped({"type": "thought", "text": "reasoning about the tool call"}),
            {"role": "assistant", "content": "ok"},
        ])
        self._sanitiser(ctx)._sanitise(ctx)
        assert len(ctx.messages) == 2
        assert all("role" in (message_dict(m) or {}) for m in ctx.messages)

    def test_a_clean_context_is_left_untouched(self):
        original = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        ctx = self._Ctx(original)
        self._sanitiser(ctx)._sanitise(ctx)
        assert ctx.messages == original

    def test_tool_plumbing_survives(self):
        """The guard must not eat the tool call it arrived alongside."""
        ctx = self._Ctx([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "x"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1"}]},
        ])
        self._sanitiser(ctx)._sanitise(ctx)
        assert len(ctx.messages) == 2

    def test_the_result_converts_cleanly_for_anthropic(self):
        from pipecat.adapters.services.anthropic_adapter import AnthropicLLMAdapter

        ctx = self._Ctx([
            {"role": "user", "content": "start a note"},
            wrapped({"type": "thought", "text": "hmm"}),
            {"role": "assistant", "content": "ok"},
        ])
        self._sanitiser(ctx)._sanitise(ctx)
        assert AnthropicLLMAdapter()._from_universal_context_messages(ctx.messages).messages
