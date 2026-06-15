"""Frame processors for the voice coding cockpit pipeline."""

import logging
import os
import sys

from pipecat.frames.frames import (
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.models import ServerMessage
from pipecat.services.kokoro.tts import KokoroTTSService
from loguru import logger

from helpers import _split_for_tts
from doc_writer import AttributedUtterance


class SafeKokoroTTSService(KokoroTTSService):
    """Kokoro wrapper that chunks long text before synthesis.

    kokoro_onnx truncates input to 510 phonemes and then indexes ``voice[len(tokens)]``,
    which raises ``IndexError`` whenever the input is that long (the error surfaces in a
    background task and crashes audio generation). Splitting into short chunks keeps every
    synthesis call well under the limit so the bug is never reached.
    """

    _TTS_MAX_CHARS = 240

    async def run_tts(self, text: str, context_id: str):
        for chunk in _split_for_tts(text, self._TTS_MAX_CHARS):
            async for frame in super().run_tts(chunk, context_id):
                yield frame


class InterceptHandler(logging.Handler):
    def emit(self, record):
        logger.opt(depth=6, exception=record.exc_info).log(record.levelname, record.getMessage())


class CockpitPrinter(FrameProcessor):
    """Assembles LLM token stream, logs responses, and sends bot-transcription to UI."""

    def __init__(self):
        super().__init__()
        self._buffer: list[str] = []
        self._task: object = None
        self._context: object = None
        self._doc_sm: object = None

    def set_task_context(self, task, context, doc_sm=None):
        self._task = task
        self._context = context
        self._doc_sm = doc_sm

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[USER]: {frame.text}")
            if self._doc_sm:
                session = self._doc_sm.session
                if session.state.value == "doc_mode" and session.doc_writer:
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
                logger.info(f"[CONTROLLER]: {text}")
                self._buffer = []
                if self._doc_sm:
                    session = self._doc_sm.session
                    if session.state.value == "doc_mode" and session.doc_writer:
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
    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    @property
    def provider(self) -> str:
        if self.model in _ANTHROPIC_MODELS:
            return "anthropic"
        if self.model in _OLLAMA_MODELS:
            return "ollama"
        return "openai"


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
