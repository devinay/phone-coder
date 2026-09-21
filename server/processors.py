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
from pipecat.services.openai.stt import OpenAISTTService

from doc_writer import AttributedUtterance
from helpers import _split_for_tts
from terminal_vocab import normalise_transcript


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


class GroveSTTService(OpenAISTTService):
    """OpenAI-compatible STT pointed at the Grove gateway.

    Grove authenticates with an ``api-key`` header rather than a bearer token,
    and pipecat builds its client without one, so the client construction is
    overridden rather than the transcription logic.

    Note this is segmented transcription: audio is sent once the VAD closes an
    utterance, so unlike Deepgram there are no interim results and latency is
    paid per utterance rather than streamed.
    """

    def __init__(self, *, api_key: str, base_url: str, **kwargs):
        self._grove_api_key = api_key
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)

    def _create_client(self, api_key: str | None, base_url: str | None):
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"api-key": self._grove_api_key},
        )


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
        if not messages or messages[0].get("role") != "system":
            return
        base = messages[0]["content"].split(self.MARKER)[0]
        messages[0] = {"role": "system", "content": base + self.MARKER + status}
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
    def __init__(self, model: str = DEFAULT_MODEL, grove_models: set[str] | None = None):
        self.model = model
        # Non-empty only when GROVE_ENABLED; keeps the flag-off path untouched.
        self.grove_models = grove_models or set()

    @property
    def provider(self) -> str:
        if self.model in self.grove_models:
            return "grove"
        if self.model in _ANTHROPIC_MODELS:
            return "anthropic"
        if self.model in _OLLAMA_MODELS:
            return "ollama"
        return "openai"

    @property
    def vendor_family(self) -> str:
        """Upstream vendor of the active model.

        For direct providers this is the provider itself. For Grove it is the
        vendor behind the gateway, which is what decides whether a model switch
        can safely preserve context.
        """
        if self.provider != "grove":
            return self.provider
        from grove import vendor_family

        return vendor_family(self.model)


class LLMCallInspector(FrameProcessor):
    """Logs a cost/model declaration before every LLM call."""

    def __init__(self, state: ModelState, output_cap: int = 512):
        super().__init__()
        self._state = state
        self._output_cap = output_cap

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            model = self._state.model
            messages = frame.context.messages or []
            text = " ".join(
                (m.get("content") or "")
                if isinstance(m.get("content"), str)
                else " ".join(
                    b.get("text", "") for b in m.get("content", []) if isinstance(b, dict)
                )
                for m in messages
            )
            est_input = max(1, len(text) // 4)
            in_price, out_price = _MODEL_PRICING.get(model, (0.0, 0.0))
            est_cost = (est_input * in_price + self._output_cap * out_price) / 1_000_000
            purpose = next(
                (m.get("content", "")[:60] for m in reversed(messages) if m.get("role") == "user"),
                "unknown",
            )
            if isinstance(purpose, list):
                purpose = purpose[0].get("text", "")[:60] if purpose else "unknown"
            alt = _CHEAPER_ALTERNATIVE.get(model, "—")
            logger.info(
                f"[LLM CALL] model={model} | purpose='{purpose}' | "
                f"~input={est_input}tok | output_cap={self._output_cap}tok | "
                f"~cost=${est_cost:.5f} | cheaper_alt={alt}"
            )

        await self.push_frame(frame, direction)
