"""Frame processors for the voice coding cockpit pipeline."""

import asyncio
import logging
import os

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.models import ServerMessage
from pipecat.services.kokoro.tts import KokoroTTSService

from doc_writer import AttributedUtterance
from helpers import _split_for_tts, is_renderable_message, message_dict
from terminal_vocab import normalise_transcript


def set_prompt_block(content: str, marker: str, body: str) -> str:
    """Replace one delimited block in the system prompt, leaving the others.

    Several processors each maintain a block. The obvious implementation —
    split on your own marker and append — silently truncates every block that
    comes after yours, so whichever processor runs last wins and the others
    vanish. A block therefore ends where the next one begins, and only the
    matching block is rewritten.

    Markers all start with a blank line and a bracket, which is what makes the
    boundary findable without tracking who owns what.
    """
    if marker in content:
        head, rest = content.split(marker, 1)
        nxt = rest.find("\n\n[")
        return head + marker + body + (rest[nxt:] if nxt != -1 else "")
    return content + marker + body


class SafeKokoroTTSService(KokoroTTSService):
    """Kokoro wrapper that chunks long text before synthesis.

    kokoro_onnx truncates input to 510 phonemes and then indexes ``voice[len(tokens)]``,
    which raises ``IndexError`` whenever the input is that long (the error surfaces in a
    background task and crashes audio generation). Splitting into short chunks keeps every
    synthesis call well under the limit so the bug is never reached.
    """

    _TTS_MAX_CHARS = 240
    # Synthesis latency scales with chunk length, and Kokoro carries no state
    # between calls, so a short opener is pure latency win: measured on an M5
    # Max (fp16), 32 chars → 0.23s to first audio, 48 → 0.28s, 81 → 0.42s.
    # Later chunks stay long because playback of one far outlasts synthesis of
    # the next (RTF ≈ 0.09), so only the opener is on the critical path.
    _TTS_FIRST_CHUNK_CHARS = 35

    def __init__(self, *, speed: float | None = None, **kwargs):
        """Wrap the engine so every synthesis runs at the configured speed.

        pipecat's ``run_tts`` hardcodes ``speed=1.0``; overriding the whole
        method to change one argument would mean copying its frame handling, so
        the rate is injected at the engine call instead.
        """
        super().__init__(**kwargs)
        self._speed = speed if speed is not None else float(os.getenv("KOKORO_SPEED", "1.25"))
        _create_stream = self._kokoro.create_stream

        async def _create_stream_at_speed(text, voice, speed=1.0, **kw):
            async for item in _create_stream(text, voice=voice, speed=self._speed, **kw):
                yield item

        self._kokoro.create_stream = _create_stream_at_speed
        logger.info(f"[KOKORO] speech rate {self._speed}x")

    async def start(self, frame):
        """Warm the ONNX graph so the session's first reply isn't the slow one.

        The very first synthesis after load costs ~0.75s against ~0.28s
        steady-state; running a throwaway phrase at startup moves that cost off
        the user's first turn.
        """
        await super().start(frame)
        try:
            voice = getattr(self._settings, "voice", None) or "af_heart"
            await asyncio.to_thread(self._kokoro.create, "Ready.", voice)
            logger.debug("[KOKORO] prewarmed")
        except Exception as e:  # never block startup on a warmup failure
            logger.warning(f"[KOKORO] prewarm skipped: {e}")

    async def run_tts(self, text: str, context_id: str):
        for chunk in _split_for_tts(text, self._TTS_MAX_CHARS, self._TTS_FIRST_CHUNK_CHARS):
            async for frame in super().run_tts(chunk, context_id):
                yield frame


class InterceptHandler(logging.Handler):
    def emit(self, record):
        logger.opt(depth=6, exception=record.exc_info).log(record.levelname, record.getMessage())


class TranscriptNormaliser(FrameProcessor):
    """Repairs terminal vocabulary the recogniser reliably mishears.

    Sits directly after the STT service so every downstream consumer — the user
    aggregator, the LLM context, doc mode's transcript — sees the corrected
    text. Correcting further downstream would leave the raw "cloud" recorded in
    some places and not others.
    """

    def __init__(self):
        super().__init__()
        self._repairs = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            repaired, changed = normalise_transcript(frame.text)
            if changed:
                self._repairs += 1
                logger.info(
                    f"[STT REPAIR] {frame.text!r} -> {repaired!r} (total {self._repairs})"
                )
                frame.text = repaired
        await self.push_frame(frame, direction)


class TerminalStatusInjector(FrameProcessor):
    """Keeps a live description of the terminal in the system prompt.

    Agents used to learn what was running from a tool result, and tool results
    are stripped from context on every agent handoff — so "claude is running"
    survived until the next switch and no further. The controller, which owns no
    terminal tools at all, could never know.

    Refreshing a block inside the system message on every turn means the fact is
    always present, for every agent, and cannot be forgotten. The block is
    delimited and rewritten in place rather than appended, so it does not
    accumulate.
    """

    MARKER = "\n\n[LIVE TERMINAL STATE — refreshed each turn]\n"

    def __init__(self, router, context):
        super().__init__()
        self._router = router
        self._context = context
        self._last = ""

    def _refresh(self) -> None:
        try:
            status = self._router.terminal_context_block()
        except Exception as e:  # never let a probe failure break a turn
            logger.warning(f"[TERMINAL STATUS] probe failed: {e}")
            return
        messages = getattr(self._context, "messages", None)
        first = message_dict(messages[0]) if messages else None
        if not first or first.get("role") != "system":
            return
        messages[0] = {
            "role": "system",
            "content": set_prompt_block(first["content"], self.MARKER, status),
        }
        self._context.set_messages(messages)
        if status != self._last:
            logger.info(f"[TERMINAL STATUS] {status}")
            self._last = status

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        # Both paths lead to an LLM call: a user utterance, and the monitor
        # waking the LLM on its own.
        if isinstance(frame, (TranscriptionFrame, LLMRunFrame)):
            if isinstance(frame, TranscriptionFrame):
                # Tells the monitor to keep quiet: while the user is talking, the
                # turn boundary services the terminal anyway.
                self._router.note_user_spoke()
            self._refresh()
        await self.push_frame(frame, direction)


class ActiveModelInjector(FrameProcessor):
    """Keeps the models actually in use in the system prompt.

    Asked which model it is running, a model with no data will not say "I do not
    know" — it produces the most plausible name it has read about, confidently.
    It has no introspective access to which weights are serving it, and the
    newest models were released after their own training data ended, so they
    have never read anything about themselves.

    But which model is configured is not self-knowledge at all; it is a setting,
    like the working directory. The only reason it could not be answered was
    that nothing put it in front of the model. Injected here, answering becomes
    reading rather than recall.

    Generated from the live state every turn and never cached: a block that
    drifted from what is actually running would be worse than no block, because
    it would be believed.
    """

    MARKER = "\n\n[ACTIVE MODELS — refreshed each turn, trust over memory]\n"

    def __init__(self, model_state, context, vision=None, catalog=None):
        super().__init__()
        self._state = model_state
        self._context = context
        # Callable returning the vision model id, or None when the feature is
        # not wired up — so this stays useful before the sketch loop exists.
        self._vision = vision
        self._catalog = catalog or {}
        self._last = ""

    def _line(self, role: str, model_id: str, note: str = "") -> str:
        info = self._catalog.get(model_id)
        bits = []
        if info is not None:
            bits.append(info.provider)
            if info.price:
                bits.append(f"${info.price[0]:g}/${info.price[1]:g} per Mtok")
        detail = f" ({', '.join(bits)})" if bits else ""
        return f"{role}: {model_id}{detail}{(' — ' + note) if note else ''}"

    def describe(self) -> str:
        lines = [self._line("conversation", self._state.model)]
        if self._vision is not None:
            chosen, source = self._vision()
            lines.append(self._line("vision", chosen, source))
        return "\n".join(lines)

    def _refresh(self) -> None:
        try:
            block = self.describe()
        except Exception as e:  # never let this break a turn
            logger.warning(f"[ACTIVE MODEL] could not describe: {e}")
            return
        # Logged when it changes, so "the model said it could not see the block"
        # can be told apart from "the injector never ran" without guessing.
        if block != self._last:
            logger.info(f"[ACTIVE MODEL] {block.replace(chr(10), ' | ')}")
            self._last = block
        messages = getattr(self._context, "messages", None)
        first = message_dict(messages[0]) if messages else None
        if not first or first.get("role") != "system":
            return
        messages[0] = {
            "role": "system",
            "content": set_prompt_block(first["content"], self.MARKER, block),
        }
        self._context.set_messages(messages)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (TranscriptionFrame, LLMRunFrame)):
            self._refresh()
        await self.push_frame(frame, direction)


class CockpitPrinter(FrameProcessor):
    """Assembles LLM token stream, logs responses, and sends bot-transcription to UI."""

    def __init__(self, runtime=None):
        super().__init__()
        self._buffer: list[str] = []
        self._task: object = None
        self._context: object = None
        self._doc_sm: object = None
        # Optional. Without it every reply is logged as [CONTROLLER], which is
        # actively misleading when a worker agent is the one talking — a refusal
        # from `doc` read as the controller refusing.
        self._runtime = runtime

    def _speaker(self) -> str:
        if self._runtime is None:
            return "CONTROLLER"
        return str(getattr(self._runtime, "active_agent_id", "controller")).upper()

    def set_task_context(self, task, context, doc_sm=None):
        self._task = task
        self._context = context
        self._doc_sm = doc_sm

    async def _send_final_text(self, text: str) -> None:
        """Give the browser the finished reply, whatever the RTVI observer did.

        `bot-transcription` is emitted by pipecat's observer only when the text
        it has accumulated matches `match_endofsentence` — and it is never
        flushed when the response ends. So a reply with no trailing `.`/`?`/`!`
        never reaches the browser at all: an answer like "`~/m/phone-coder`"
        arrived as an empty bubble, and the text stayed buffered to be prepended
        to the following reply.

        Rather than depend on that, the assembled response is sent here on its
        own channel. The cockpit renders it only if nothing was displayed for
        this turn, so a normal prose reply is not shown twice.
        """
        if self._task is None or not text.strip():
            return
        msg = ServerMessage(data={"type": "bot-text-final", "text": text})
        try:
            await self.push_frame(
                OutputTransportMessageUrgentFrame(message=msg.model_dump()),
                FrameDirection.DOWNSTREAM,
            )
        except Exception as e:
            logger.warning(f"[COCKPIT] could not send the final reply text: {e}")

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[USER]: {frame.text}")
            if self._doc_sm:
                session = self._doc_sm.session
                if session.state.value in ("doc_mode", "diagram_focus") and session.doc_writer:
                    import time as _time

                    session.doc_writer.add_utterance(
                        AttributedUtterance(
                            text=frame.text,
                            timestamp=_time.time(),
                            speaker_id="user",
                            confidence=None,
                        )
                    )
                    session.doc_writer.set_speaker_map(session.speaker_map)
        elif isinstance(frame, LLMFullResponseStartFrame):
            logger.info("[COCKPIT] LLM response started")
            self._buffer = []
        elif isinstance(frame, LLMTextFrame):
            self._buffer.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            logger.info("[COCKPIT] LLM response ended")
            if self._buffer:
                text = "".join(self._buffer)
                logger.info(f"[{self._speaker()}]: {text}")
                self._buffer = []
                await self._send_final_text(text)
                if self._doc_sm:
                    session = self._doc_sm.session
                    if session.state.value in ("doc_mode", "diagram_focus") and session.doc_writer:
                        import time as _time

                        session.doc_writer.add_utterance(
                            AttributedUtterance(
                                text=text,
                                timestamp=_time.time(),
                                speaker_id="controller",
                                confidence=None,
                            )
                        )

        await self.push_frame(frame, direction)


class TTSState:
    def __init__(self):
        self.enabled = os.getenv("TTS_ENABLED", "false").lower() == "true"
        self.provider = os.getenv("TTS_PROVIDER", "cartesia")  # "cartesia" | "openai" | "kokoro" | "deepgram"
        self.last_error = ""

    def set_enabled(self, enabled: bool, reason: str = ""):
        self.enabled = enabled
        if not enabled and reason:
            self.last_error = reason


class TTSGate(FrameProcessor):
    """Marks response text as skip_tts unless browser voice mode is enabled."""

    def __init__(self, state: TTSState):
        super().__init__()
        self._state = state

    async def _send_status(self, reason: str = ""):
        msg = ServerMessage(
            data={
                "type": "tts-status",
                "enabled": self._state.enabled,
                "provider": self._state.provider,
                "reason": reason,
            }
        )
        await self.push_frame(
            OutputTransportMessageUrgentFrame(message=msg.model_dump()),
            FrameDirection.DOWNSTREAM,
        )

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, ErrorFrame) and self._state.enabled:
            error_text = frame.error or ""
            if "Cartesia" in error_text or "TTS" in error_text or "402" in error_text:
                logger.warning(f"TTS error; falling back to text-only: {error_text}")
                self._state.set_enabled(False, error_text)
                await self._send_status("TTS error; voice disabled for future replies.")

        if not self._state.enabled and isinstance(
            frame,
            (TextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame),
        ):
            frame.skip_tts = True

        await self.push_frame(frame, direction)


# ── Model catalogue ────────────────────────────────────────────────────────────

# (input_price, output_price) per 1M tokens, USD — approximate 2026 rates
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (15.00, 75.00),
    "qwen2.5-coder:7b": (0.00, 0.00),  # local/free
}

_CHEAPER_ALTERNATIVE: dict[str, str] = {
    "gpt-4o": "gpt-4o-mini",
    "gpt-4.1": "gpt-4.1-mini",
    "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
    "claude-opus-4-8": "claude-sonnet-4-6",
}

_OPENAI_MODELS = {"gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"}
_ANTHROPIC_MODELS = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"}
_OLLAMA_MODELS = {"qwen2.5-coder:7b"}

DEFAULT_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")


class ModelState:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        providers: dict[str, str] | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
    ):
        self.model = model
        # id -> (input, output) USD per Mtok, from the catalogue. Kept beside
        # the provider map so the cost line tracks the same source the picker
        # shows the user, rather than a second list that drifts from it.
        self.prices = prices or {}
        # id -> provider, from the fetched catalogue. The hardcoded sets below
        # remain as the fallback for when a provider could not be reached, so a
        # network failure narrows the choice rather than misrouting it.
        self.providers = providers or {}

    @property
    def provider(self) -> str:
        known = self.providers.get(self.model)
        if known:
            return known
        if self.model in _ANTHROPIC_MODELS:
            return "anthropic"
        if self.model in _OLLAMA_MODELS:
            return "ollama"
        return "openai"

    @property
    def vendor_family(self) -> str:
        """Upstream vendor of the active model.

        Every model now reaches its vendor directly, so the vendor and the
        provider are the same thing. This stays a separate name because what
        callers actually want to know is whether a model switch can preserve
        context, and that is a question about the vendor.
        """
        return self.provider


class ContextSanitiser(FrameProcessor):
    """Drops context messages no provider can turn into a request.

    Pipecat's Anthropic adapter passes provider-specific messages through
    verbatim and then reads ``message["role"]``, so one entry without a role
    kills the turn with ``KeyError('role')`` — naming neither the message nor
    its origin. Anthropic's own reasoning artifacts can take that shape, which
    means a model's output can poison its next turn.

    Sits ahead of the LLM and removes those entries. Deliberately narrow: it
    drops only what is provably unrenderable, never anything merely unfamiliar,
    because silently shortening the conversation is its own class of bug. What
    it drops is logged, since a message vanishing from a conversation should
    never be invisible.
    """

    def __init__(self, context):
        super().__init__()
        self._context = context

    def _sanitise(self, context=None) -> None:
        target = context if context is not None else self._context
        messages = getattr(target, "messages", None)
        if not messages:
            return
        kept = [m for m in messages if is_renderable_message(m)]
        dropped = len(messages) - len(kept)
        if dropped:
            logger.warning(
                f"[CONTEXT] dropped {dropped} message(s) no provider can render "
                "(most likely a reasoning artifact without a role)"
            )
            target.set_messages(kept)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        # LLMContextFrame matters most. A tool result pushes one straight back
        # to the LLM without a new user turn, so gating on transcription alone
        # left the whole tool-calling path unguarded — which is exactly where
        # this bites, since a thought and a tool call arrive in the same turn.
        if isinstance(frame, LLMContextFrame):
            try:
                self._sanitise(frame.context)
            except Exception as e:  # never fail a turn while protecting it
                logger.warning(f"[CONTEXT] sanitise skipped: {e}")
        elif isinstance(frame, (TranscriptionFrame, LLMRunFrame)):
            try:
                self._sanitise()
            except Exception as e:
                logger.warning(f"[CONTEXT] sanitise skipped: {e}")
        await self.push_frame(frame, direction)


class LLMCallInspector(FrameProcessor):
    """Logs a cost/model declaration before every LLM call."""

    def __init__(self, state: ModelState, output_cap: int = 512):
        super().__init__()
        self._state = state
        self._output_cap = output_cap

    def describe_call(self, messages) -> str:
        """The cost line for a pending call.

        Separated from frame handling so it can be tested directly. The first
        fix for the LLMSpecificMessage crash introduced a second crash here that
        every test missed, because the tests covered the helper this calls and
        never ran the processor.
        """
        model = self._state.model
        # Messages are not uniformly dicts — see helpers.message_dict. Anything
        # unreadable is skipped: a cost estimate is worth less than the turn it
        # would otherwise take down.
        dicts = [d for d in (message_dict(m) for m in messages or []) if d]
        text = " ".join(
            (d.get("content") or "")
            if isinstance(d.get("content"), str)
            else " ".join(
                b.get("text", "") for b in d.get("content") or [] if isinstance(b, dict)
            )
            for d in dicts
        )
        est_input = max(1, len(text) // 4)
        in_price, out_price = self._state.prices.get(
            model, _MODEL_PRICING.get(model, (0.0, 0.0))
        )
        est_cost = (est_input * in_price + self._output_cap * out_price) / 1_000_000
        purpose = next(
            (d.get("content", "") for d in reversed(dicts) if d.get("role") == "user"),
            "unknown",
        )
        if isinstance(purpose, list):
            purpose = purpose[0].get("text", "") if purpose else "unknown"
        alt = _CHEAPER_ALTERNATIVE.get(model, "—")
        return (
            f"[LLM CALL] model={model} | purpose='{str(purpose)[:60]}' | "
            f"~input={est_input}tok | output_cap={self._output_cap}tok | "
            f"~cost=${est_cost:.5f} | cheaper_alt={alt}"
        )

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            try:
                logger.info(self.describe_call(frame.context.messages))
            except Exception as e:
                # Cost logging is never worth failing a turn for.
                logger.warning(f"[LLM CALL] could not describe the call: {e}")

        await self.push_frame(frame, direction)
