#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Voice Coding Cockpit — bot server

Pipeline: Deepgram STT → GPT-4o (with tools) → Cartesia TTS

Run with:  uv run bot.py
Open:      http://localhost:7860/cockpit
"""

import asyncio
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    ManuallySwitchServiceFrame,
    OutputTransportMessageUrgentFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.models import (
    ServerMessage,
)
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from agent_router import AgentRouter
from diagram_focus import DiagramFocusStateMachine
from doc_state import ALREADY_ACTIVE, INVALID_STATE, DocStateMachine, StateMachineError
from doc_storage import (
    atomic_write,
    create_project,
    docs_root,
    fork_version,
    list_projects,
    load_project,
    load_version,
)
from doc_writer import AttributedUtterance

load_dotenv(override=True)


_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _find_section(lines: list[str], section: str) -> tuple[int | None, int]:
    """Find a markdown header (## .. ######) whose title equals `section`.

    Returns (start_index, level). start_index is None if not found.
    """
    target = section.strip()
    for i, l in enumerate(lines):
        m = _MD_HEADER_RE.match(l)
        if m and len(m.group(1)) >= 2 and m.group(2).strip() == target:
            return i, len(m.group(1))
    return None, 2


def _section_end(lines: list[str], start: int, level: int) -> int | None:
    """Index of the next header at the same or shallower level after `start`, or None."""
    for i in range(start + 1, len(lines)):
        m = _MD_HEADER_RE.match(lines[i])
        if m and len(m.group(1)) <= level:
            return i
    return None


def _replace_section(doc: str, section: str, new_content: str) -> str:
    """Replace the body under a markdown header named `section` (any level ##–######),
    or append a new ## section if none exists. Matching by name (not level) means an
    existing ### subsection is edited in place instead of spawning a duplicate ## header.
    """
    lines = doc.splitlines(keepends=True)
    start, level = _find_section(lines, section)
    if start is None:
        header = f"## {section}"
        sep = "\n\n" if doc and not doc.endswith("\n\n") else ""
        return doc + sep + header + "\n\n" + new_content.strip() + "\n"
    end = _section_end(lines, start, level)
    before = "".join(lines[: start + 1])
    after = "".join(lines[end:]) if end is not None else ""
    return before + "\n\n" + new_content.strip() + "\n\n" + after


def _strip_generated_sections(doc: str) -> str:
    """Remove a previously generated ``## Summary`` block and trailing Transcript
    ``<details>`` so re-saving an opened document replaces them instead of stacking
    duplicates. Content the user authored in between is left untouched.
    """
    # Trailing transcript collapsible (plus any --- separator that precedes it).
    doc = re.sub(
        r"\n*(?:---[ \t]*\n+)?<details>\s*<summary>Transcript</summary>.*?</details>\s*$",
        "\n",
        doc,
        flags=re.DOTALL,
    )
    # Leading "## Summary ... \n---\n" block (only the first occurrence).
    doc = re.sub(
        r"## Summary\n.*?\n---[ \t]*\n+",
        "",
        doc,
        count=1,
        flags=re.DOTALL,
    )
    return doc.rstrip() + "\n"


# ── Mermaid diagram helpers ───────────────────────────────────────────────────

# Diagram types the Mermaid CDN version supports reliably in production
MERMAID_SUPPORTED_TYPES = {
    "flowchart", "graph", "sequenceDiagram", "classDiagram",
    "stateDiagram", "stateDiagram-v2", "erDiagram", "gantt",
    "pie", "gitGraph", "journey", "mindmap", "timeline",
    "block-beta", "quadrantChart",
}

# Types that are beta/unstable — agent must re-prompt with a supported fallback
MERMAID_UNSUPPORTED_TYPES = {
    "xychart-beta", "sankey-beta", "C4Context", "C4Container", "C4Component",
}

# Pattern that matches a diagram block in document.md:
#   <!-- diagram-id: <id> -->
#   ```mermaid
#   <source>
#   ```
_DIAGRAM_BLOCK_RE = re.compile(
    r'(<!-- diagram-id: (?P<id>[a-z0-9_-]+) -->\n```mermaid\n)(?P<src>.*?)```',
    re.DOTALL,
)

# Pattern that matches a placeholder inserted by the agent in Phase 1:
#   <!-- diagram: <description> -->
_DIAGRAM_PLACEHOLDER_RE = re.compile(r'<!-- diagram: (?P<desc>[^>]+?) -->')


def _validate_mermaid_source(source: str, diagram_type: str) -> tuple[bool, str]:
    """Check that source starts with the declared diagram type keyword.

    Returns (is_valid, error_message).
    """
    stripped = source.strip()
    valid_starts = {diagram_type}
    if diagram_type == "flowchart":
        valid_starts.add("graph")
    for start in valid_starts:
        if stripped.lower().startswith(start.lower()):
            return True, ""
    first_line = stripped.splitlines()[0] if stripped else "(empty)"
    return False, (
        f"Source first line '{first_line}' does not match declared type '{diagram_type}'. "
        f"Mermaid source must begin with the diagram keyword."
    )


def _build_diagram_block(diagram_id: str, mermaid_source: str) -> str:
    return f"<!-- diagram-id: {diagram_id} -->\n```mermaid\n{mermaid_source.strip()}\n```"


def _insert_diagram_in_doc(
    doc: str, diagram_id: str, mermaid_source: str, replace_placeholder: str | None
) -> str:
    """Insert a new diagram block, optionally replacing a placeholder comment."""
    block = _build_diagram_block(diagram_id, mermaid_source)
    if replace_placeholder:
        # Try exact text match first, then partial match
        placeholder = f"<!-- diagram: {replace_placeholder} -->"
        if placeholder in doc:
            return doc.replace(placeholder, block, 1)
        # Partial match: find any placeholder whose description contains the text
        m = _DIAGRAM_PLACEHOLDER_RE.search(doc)
        if m:
            return doc[:m.start()] + block + doc[m.end():]
    # No placeholder: append before the last newline or at end
    sep = "\n\n" if doc and not doc.endswith("\n\n") else ""
    return doc + sep + block + "\n"


def _update_diagram_in_doc(doc: str, diagram_id: str, mermaid_source: str) -> tuple[str, bool]:
    """Replace the mermaid source inside an existing diagram block.

    Returns (new_doc, found).
    """
    block = _build_diagram_block(diagram_id, mermaid_source)

    new_doc, count = _DIAGRAM_BLOCK_RE.subn(
        lambda m: block if m.group("id") == diagram_id else m.group(0),
        doc,
    )
    return new_doc, count > 0


def _extract_diagram_source(doc: str, diagram_id: str) -> str | None:
    """Extract the mermaid source for a diagram block by ID. Returns None if not found."""
    for m in _DIAGRAM_BLOCK_RE.finditer(doc):
        if m.group("id") == diagram_id:
            return m.group("src").strip()
    return None


def _move_diagram_in_doc(doc: str, diagram_id: str, target_section: str) -> tuple[str, str]:
    """Relocate the (single, marked) diagram block to the end of `target_section`'s body.

    Removes the block from its current position and re-inserts it — preserving the
    ``<!-- diagram-id -->`` marker — so the diagram never ends up duplicated.

    Returns (new_doc, status) where status is one of:
      "ok"           — moved successfully
      "no_diagram"   — no block with that id
      "no_section"   — target section header not found (doc unchanged)
    """
    match = next((m for m in _DIAGRAM_BLOCK_RE.finditer(doc) if m.group("id") == diagram_id), None)
    if match is None:
        return doc, "no_diagram"
    block = doc[match.start():match.end()].strip()

    # Remove the block from its current location, collapsing the surrounding blank lines.
    without = (doc[:match.start()].rstrip() + "\n\n" + doc[match.end():].lstrip()).strip() + "\n"

    lines = without.splitlines(keepends=True)
    start, level = _find_section(lines, target_section)
    if start is None:
        return doc, "no_section"  # leave the original untouched

    end = _section_end(lines, start, level)
    end_idx = end if end is not None else len(lines)
    before = "".join(lines[:end_idx]).rstrip()
    after = "".join(lines[end_idx:]).lstrip()
    new_doc = before + "\n\n" + block + "\n\n" + after
    return new_doc.rstrip() + "\n", "ok"


# ── End diagram helpers ───────────────────────────────────────────────────────


def _split_for_tts(text: str, max_len: int) -> list[str]:
    """Split text into chunks no longer than max_len, preferring sentence then word
    boundaries. Keeps each chunk well under Kokoro's phoneme cap.
    """
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else []

    # First split on sentence boundaries, then pack greedily up to max_len.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        while len(s) > max_len:
            # A single over-long sentence: break on the last space before max_len.
            cut = s.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= max_len:
            cur = f"{cur} {s}"
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


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


TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() == "true"
TTYD_PORT = int(os.getenv("TTYD_PORT", "7681"))
TTYD_BASE = f"http://127.0.0.1:{TTYD_PORT}"
TTYD_WS_URL = f"ws://127.0.0.1:{TTYD_PORT}/ws"

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logger.remove()  # remove default stdout sink
logger.add(sys.stdout, level="DEBUG", colorize=True)
logger.add(
    os.path.join(_LOG_DIR, "bot.log"),
    rotation="10 MB",
    retention="5 days",
    level="DEBUG",
    enqueue=True,
)


logger.info(f"Log file: {os.path.join(_LOG_DIR, 'bot.log')}")


class InterceptHandler(logging.Handler):
    def emit(self, record):
        logger.opt(depth=6, exception=record.exc_info).log(record.levelname, record.getMessage())


logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

for noisy in ("aiortc", "aioice", "aiohttp.client_ws", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class SpeakerIdentificationGate(FrameProcessor):
    """Blocks utterances from unknown speakers until they identify themselves.

    In doc_mode, when a new speaker is detected, this gate:
    1. Suppresses their utterances from entering the LLM context
    2. Repeatedly prompts them to identify themselves via set_speaker_name
    3. Only unblocks once set_speaker_name is called
    """

    def __init__(self):
        super().__init__()
        self._task: object = None
        self._context: object = None
        self._transcription_frames: dict[str, TranscriptionFrame] = {}
        # Speakers currently awaiting identification: {speaker_id: frame}
        self._speakers_awaiting_id: dict[str, TranscriptionFrame] = {}

    def set_task_context(self, task, context):
        self._task = task
        self._context = context

    async def process_frame(self, frame, direction):
        # Check if we need to block/suppress this LLMContextFrame
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.UPSTREAM:
            session = _doc_sm.session
            logger.debug(f"[SPEAKER GATE] LLMContextFrame received, state={session.state.value}, direction={direction}")
            if session.state.value == "doc_mode":
                # Extract the transcription that was just added to context
                # The transcription was set as the last user message
                if self._context and self._context.messages:
                    last_msg = self._context.messages[-1]
                    if last_msg.get("role") == "user":
                        # Find which speaker_id this came from by checking our buffer
                        speaker_id = getattr(self, "_last_speaker_id", None)
                        if speaker_id and speaker_id in self._speakers_awaiting_id:
                            # This is from a speaker awaiting ID — block it
                            logger.info(
                                f"[SPEAKER GATE] Blocking utterance from speaker {speaker_id} "
                                f"(awaiting identification): {last_msg.get('content')[:100]}..."
                            )
                            # Remove the message that was just added
                            self._context.messages.pop()
                            # Re-prompt for identification
                            self._context.add_message({
                                "role": "user",
                                "content": (
                                    f"[SYSTEM NOTE] Speaker {speaker_id} is still unidentified. "
                                    f"Please ask them again who they are and call set_speaker_name with their name."
                                ),
                            })
                            await self._task.queue_frames([LLMRunFrame()])
                            return  # Don't pass this frame downstream
                logger.debug(f"[SPEAKER GATE] Not in doc_mode, passing frame through")
            else:
                logger.debug(f"[SPEAKER GATE] Not in doc_mode, passing frame through")

        await super().process_frame(frame, direction)

    async def record_transcription(self, frame: TranscriptionFrame, speaker_id: str, session):
        """Called by CockpitPrinter to record a new transcription."""
        self._transcription_frames[speaker_id] = frame
        self._last_speaker_id = speaker_id

        # Check if this is an unknown speaker in doc_mode
        if (
            speaker_id is not None
            and speaker_id not in session.speaker_map
            and self._task is not None
            and self._context is not None
        ):
            # Speaker is awaiting identification
            if speaker_id not in self._speakers_awaiting_id:
                self._speakers_awaiting_id[speaker_id] = frame
                logger.info(f"[SPEAKER GATE] New speaker detected and blocked: id={speaker_id}")
                # Immediately prompt for identification
                self._context.add_message({
                    "role": "user",
                    "content": (
                        f"[SYSTEM NOTE] A new speaker (id={speaker_id}) is speaking. "
                        f"Ask them who they are, and then immediately call set_speaker_name "
                        f"with their name (e.g., set_speaker_name('{speaker_id}', 'Alice')). "
                        f"Do not process any other commands until this speaker is identified."
                    ),
                })
                await self._task.queue_frames([LLMRunFrame()])

    async def mark_speaker_identified(self, speaker_id: str):
        """Called when set_speaker_name is invoked for a speaker."""
        if speaker_id in self._speakers_awaiting_id:
            del self._speakers_awaiting_id[speaker_id]
            logger.info(f"[SPEAKER GATE] Speaker {speaker_id} identified and unblocked")


class CockpitPrinter(FrameProcessor):
    """Assembles LLM token stream, logs responses, and sends bot-transcription to UI."""

    def __init__(self, speaker_gate: SpeakerIdentificationGate = None):
        super().__init__()
        self._buffer: list[str] = []
        self._speaker_gate = speaker_gate
        self._task: object = None
        self._context: object = None

    def set_task_context(self, task, context):
        self._task = task
        self._context = context

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            speaker = getattr(frame, "speaker", None)
            if speaker is None and frame.result is not None:
                try:
                    words = (frame.result or {}).get("words") or []
                    speakers = {w.get("speaker") for w in words if w.get("speaker") is not None}
                    speaker = ",".join(str(s) for s in sorted(speakers)) if speakers else None
                except Exception:
                    pass
            logger.info(f"[USER speaker={speaker}]: {frame.text}")
        elif isinstance(frame, LLMFullResponseStartFrame):
            logger.info("[COCKPIT] LLM response started")
        elif isinstance(frame, LLMFullResponseEndFrame):
            logger.info("[COCKPIT] LLM response ended")

            # Feed DocWriter when in doc_mode
            session = _doc_sm.session
            if session.state.value == "doc_mode" and session.doc_writer:
                import time as _time

                speaker_id = str(speaker) if speaker is not None else None
                utterance = AttributedUtterance(
                    text=frame.text,
                    timestamp=_time.time(),
                    speaker_id=speaker_id,
                    confidence=None,
                )
                session.doc_writer.add_utterance(utterance)
                session.doc_writer.set_speaker_map(session.speaker_map)

                # Notify speaker gate of this transcription
                if self._speaker_gate:
                    await self._speaker_gate.record_transcription(frame, speaker_id, session)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._buffer = []
        elif isinstance(frame, LLMTextFrame):
            self._buffer.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._buffer:
                text = "".join(self._buffer)
                logger.info(f"[CONTROLLER]: {text}")
                self._buffer = []
                session = _doc_sm.session
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
        self.enabled = TTS_ENABLED
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


# ── Global singletons ──────────────────────────────────────────────────────────

_router: AgentRouter | None = None
_ttyd_proc: subprocess.Popen | None = None
_doc_sm: DocStateMachine = DocStateMachine()
_diagram_focus_sm: DiagramFocusStateMachine = DiagramFocusStateMachine()


def get_router() -> AgentRouter:
    global _router
    if _router is None:
        _router = AgentRouter()
    return _router


def _ensure_writable_version(session):
    """Return the VersionInfo to write to, forking opened versions on first edit.

    Freshly created projects write to their own version_0 directly. Sessions that
    opened an existing version are forked to a new version on the first successful
    edit so the original version stays intact.
    """
    if session.opened_existing and not session.forked and session.version_info:
        base = session.version_info
        new_vi = fork_version(base)
        session.version_info = new_vi
        session.version = new_vi.version
        session.forked = True
        logger.info(
            f"[DOC] Copy-on-write fork: version {base.version} → version {new_vi.version}"
        )
    return session.version_info


def _mark_doc_session_edited(session) -> None:
    """Record that the active documentation session has persisted changes."""
    session.has_edits = True


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def ensure_terminal_running(port: int = TTYD_PORT) -> bool:
    """Ensure the tmux session and ttyd proxy process exist."""
    global _ttyd_proc

    router = get_router()
    router.ensure_session()

    if _is_port_open(port):
        return True

    if _ttyd_proc is not None and _ttyd_proc.returncode is None:
        return False

    if shutil.which("ttyd") is None:
        logger.error("Cannot start terminal: ttyd is not installed or not on PATH")
        return False

    _ttyd_proc = subprocess.Popen(
        [
            "ttyd",
            "--port",
            str(port),
            "--writable",
            "-t",
            "scrollback=50000",
            "tmux",
            "attach-session",
            "-t",
            AgentRouter.SESSION,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info(f"ttyd started (pid {_ttyd_proc.pid}) on port {port}")
    return False


# ── Image search helpers ───────────────────────────────────────────────────────

def _ddg_image_search(query: str, max_results: int = 5) -> list[dict]:
    """Synchronous DuckDuckGo image search. Run in a thread pool."""
    from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        return ddgs.images(query, max_results=max_results, type_image="transparent") or \
               ddgs.images(query, max_results=max_results)


def _ddg_text_search(query: str, max_results: int = 5) -> list[dict]:
    """Synchronous DuckDuckGo text/web search. Run in a thread pool.

    Returns a list of {title, body, href} result dicts (newest DDGS schema).
    """
    from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        return ddgs.text(query, max_results=max_results) or []


# ── Pluggable web-search backends ────────────────────────────────────────────
# Each backend is an async fn (query, n) -> list[{title, body, href}]. Backends
# enable themselves when their API key is present; DDG is always on (best-effort).
# web_search() fans out to every enabled backend in parallel, then merges + dedupes
# results and tags each with the backend(s) that returned it (corroboration), so the
# controller never has to make extra round-trips to "compare". Add a new source —
# another web API, or a local data repository — by writing one async fn and listing
# it in _enabled_search_backends().


async def _search_duckduckgo(query: str, n: int) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _ddg_text_search(query, n))


async def _search_tavily(query: str, n: int) -> list[dict]:
    import aiohttp

    key = os.getenv("TAVILY_API_KEY", "")
    payload = {"api_key": key, "query": query, "max_results": n, "search_depth": "basic"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post("https://api.tavily.com/search", json=payload) as r:
            r.raise_for_status()
            data = await r.json()
    return [
        {"title": x.get("title", ""), "body": x.get("content", ""), "href": x.get("url", "")}
        for x in data.get("results", [])
    ]


async def _search_brave(query: str, n: int) -> list[dict]:
    import aiohttp

    key = os.getenv("BRAVE_API_KEY", "")
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    params = {"q": query, "count": n}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(
            "https://api.search.brave.com/res/v1/web/search", params=params, headers=headers
        ) as r:
            r.raise_for_status()
            data = await r.json()
    return [
        {"title": x.get("title", ""), "body": x.get("description", ""), "href": x.get("url", "")}
        for x in data.get("web", {}).get("results", [])
    ]


def _enabled_search_backends() -> list[tuple[str, object]]:
    """Return (name, async_fn) for every backend that is currently usable."""
    backends: list[tuple[str, object]] = [("duckduckgo", _search_duckduckgo)]
    if os.getenv("TAVILY_API_KEY"):
        backends.append(("tavily", _search_tavily))
    if os.getenv("BRAVE_API_KEY"):
        backends.append(("brave", _search_brave))
    # Future sources (e.g. a local document repository) plug in here.
    return backends


async def _multi_search(query: str, n: int) -> tuple[list[dict], list[str], list[str]]:
    """Fan out to all enabled backends concurrently, merge + dedupe by URL.

    Returns (results, ok_backends, failed_backends). Each result dict carries a
    "sources" list naming the backends that returned it, ordered by corroboration.
    """
    backends = _enabled_search_backends()

    async def _run(name: str, fn) -> tuple[str, object]:
        try:
            return name, await asyncio.wait_for(fn(query, n), timeout=11)
        except Exception as e:  # isolate one backend's failure from the rest
            logger.warning(f"[WEB] backend '{name}' failed: {e}")
            return name, e

    outcomes = await asyncio.gather(*[_run(name, fn) for name, fn in backends])

    merged: dict[str, dict] = {}
    ok, failed = [], []
    for name, res in outcomes:
        if isinstance(res, Exception):
            failed.append(name)
            continue
        ok.append(name)
        for item in res:
            url = (item.get("href") or "").strip()
            if not url:
                continue
            key = url.rstrip("/")
            if key not in merged:
                merged[key] = {
                    "title": item.get("title", ""),
                    "body": item.get("body", ""),
                    "href": url,
                    "sources": [],
                }
            if name not in merged[key]["sources"]:
                merged[key]["sources"].append(name)

    # Most-corroborated first (returned by the most backends), then cap to n.
    ordered = sorted(merged.values(), key=lambda m: -len(m["sources"]))[:n]
    return ordered, ok, failed


async def _download_image(
    session: "aiohttp.ClientSession",
    url: str,
    dest_stem: Path,
    n: int,
    max_bytes: int = 2 * 1024 * 1024,
    timeout: int = 8,
) -> Path:
    """Download one image, infer extension, return Path on success."""
    import mimetypes
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            ext = mimetypes.guess_extension(ct) or ".jpg"
            ext = ext.replace(".jpe", ".jpg")
            dest = dest_stem.with_suffix(ext)
            data = await resp.read()
            if len(data) > max_bytes:
                raise ValueError("Image too large")
            dest.write_bytes(data)
            return dest
    except Exception as e:
        raise RuntimeError(f"Image {n} download failed: {e}") from e


def _embed_image_in_node(
    source: str, element_id: str, img_url: str, width: int
) -> tuple[str, bool]:
    """Rewrite a Mermaid flowchart node label to embed an image tag.

    Handles common node shapes: [text], (text), {text}, [(text)], ((text)), >text].
    Returns (new_source, found).
    """
    img_tag = f'<img src="{img_url}" width="{width}"/>'
    # Match: element_id optionally followed by whitespace, then opening bracket(s), content, closing bracket(s)
    pattern = re.compile(
        rf'(?<![A-Za-z0-9_])({re.escape(element_id)}\s*)(\[{{1,2}}|\({{1,3}}|\{{|\>)([^\]\)\}}>]*)(\]{{1,2}}|\){{1,3}}|\}}|\])',
        re.DOTALL,
    )
    result, count = pattern.subn(
        lambda m: f'{m.group(1)}{m.group(2)}"{img_tag}"{m.group(4)}',
        source,
        count=1,
    )
    return result, count > 0


async def _generate_doc_summary(doc_content: str, api_key: str) -> str:
    """Call OpenAI to produce a ≤5-line plain-text summary of the document."""
    if not api_key or not doc_content.strip():
        return ""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You summarise documents. Write a plain-text summary in 5 lines or fewer. "
                        "No bullet points, no markdown, no headers. Just concise prose."
                    ),
                },
                {"role": "user", "content": f"Summarise this document:\n\n{doc_content[:4000]}"},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"[DOC] Summary generation failed: {e}")
        return ""


async def run_bot(transport: BaseTransport, ttyd_port: int = TTYD_PORT):
    """Main bot logic."""
    logger.info("Starting bot")

    # Unique ID for this bot session — used to namespace ephemeral image temp dirs
    _session_id = uuid.uuid4().hex[:12]
    _image_tmp_root = Path("/tmp/cockpit-images") / _session_id

    router = get_router()

    # Speech-to-Text service (PoC 2: diarize enabled — logs speaker field for validation)
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(diarize=True, punctuate=True, smart_format=True),
    )

    # Text-to-Speech services — switchable at runtime via UI.
    # A Markdown filter strips formatting (#, *, ```code```, tables) so the TTS speaks
    # plain prose instead of reading symbols aloud ("star", "pound"). The browser still
    # receives the full Markdown for display via the bot-transcription message.
    def _md_filter():
        return MarkdownTextFilter(
            params=MarkdownTextFilter.InputParams(filter_code=True, filter_tables=True)
        )

    tts_state = TTSState()
    tts_cartesia = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
        ),
        text_filters=[_md_filter()],
    )
    tts_openai = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAITTSService.Settings(
            voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
        ),
        text_filters=[_md_filter()],
    )
    tts_kokoro = SafeKokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=os.getenv("KOKORO_VOICE", "af_heart"),
        ),
        text_filters=[_md_filter()],
    )
    tts_deepgram = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        settings=DeepgramTTSService.Settings(
            voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en"),
        ),
        text_filters=[_md_filter()],
    )
    _tts_services = {"cartesia": tts_cartesia, "openai": tts_openai, "kokoro": tts_kokoro, "deepgram": tts_deepgram}
    tts = ServiceSwitcher(
        services=[tts_cartesia, tts_openai, tts_kokoro, tts_deepgram],
        strategy_type=ServiceSwitcherStrategyManual,
    )

    # LLM services — switchable at runtime via UI dropdown
    model_state = ModelState()
    llm_openai = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model=model_state.model if model_state.provider == "openai" else DEFAULT_MODEL,
    )
    llm_anthropic = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        model=model_state.model
        if model_state.provider == "anthropic"
        else "claude-haiku-4-5-20251001",
    )
    # Ollama exposes an OpenAI-compatible API on localhost
    llm_ollama = OpenAILLMService(
        api_key="ollama",
        model="qwen2.5-coder:7b",
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    )
    _llm_by_provider = {"openai": llm_openai, "anthropic": llm_anthropic, "ollama": llm_ollama}
    llm = ServiceSwitcher(
        services=[llm_openai, llm_anthropic, llm_ollama],
        strategy_type=ServiceSwitcherStrategyManual,
    )
    inspector = LLMCallInspector(model_state)

    async def run_command(params: FunctionCallParams, command: str, directory_path: str = ""):
        """Run a shell command in the terminal. Use this for everything: starting assistants
        (e.g. 'claude', 'codex'), running build tools, git commands, etc.

        Args:
            command: The verbatim shell command to run (e.g. 'claude', 'ls -la', 'git status')
            directory_path: Absolute path to cd into before running. Omit for commands that
                            should run in the current working directory (e.g. pwd, ls, git status,
                            cloud, claude, or any REPL/interactive tool).
        """
        result = await router.run_command(command, directory_path)
        logger.info(f"[TOOL] RUN COMMAND: {command}\n{result}")
        await params.result_callback(result)

    async def send_input(params: FunctionCallParams, text: str):
        """Send text input to whatever is currently running in the terminal.
        Use this to interact with an interactive program (e.g. sending a prompt to claude).

        Args:
            text: The text to send
        """
        result = await router.send_input(text)
        logger.info(f"[TOOL] SEND INPUT: {text}\n{result}")
        await params.result_callback(result)

    async def capture_output(params: FunctionCallParams, lines: int = 50):
        """Capture recent terminal output to check what happened.

        Args:
            lines: Number of lines to capture (default 50)
        """
        result = router.capture_output(lines)
        logger.info(f"[TOOL] CAPTURE OUTPUT\n{result}")
        await params.result_callback(result)

    async def find_directory(params: FunctionCallParams, directory_name: str):
        """Search for a directory by name up to 3 levels deep from the home directory.
        Use this when the user gives a partial name or relative path.

        Args:
            directory_name: The name or partial path of the directory to find.
        """
        path, is_exact = router.find_best_directory(directory_name)
        if not path:
            result = f"Directory '{directory_name}' not found within 3 levels."
        elif isinstance(path, list):
            result = f"Found multiple matches: {', '.join(path)}. Which one did you mean?"
        else:
            result = f"Found {'exact ' if is_exact else ''}match at '{path}'."

        print(f"\n[TOOL] FIND DIRECTORY: {directory_name}\n{result}\n")
        await params.result_callback(result)

    async def list_doc_projects(params: FunctionCallParams):
        """List all existing documentation projects.

        Call this before asking the user which project to open, so you can
        present them with the available options instead of asking for a slug blindly.
        """
        projects = list_projects()
        if not projects:
            await params.result_callback(
                f"NO_PROJECTS: No documentation projects found in {docs_root()}."
            )
            return
        lines = [
            f"- slug='{p['slug']}', name='{p['display_name']}', version={p['current_version']}"
            for p in projects
        ]
        await params.result_callback("Available projects:\n" + "\n".join(lines))

    async def enter_doc_mode(
        params: FunctionCallParams,
        action: str,
        topic_name: str = "",
        project_slug: str = "",
    ):
        """Enter Documentation Mode to create or open a documentation project.

        Call this when the user wants to start or continue a documentation session.

        Args:
            action: "create" to start a new project, "open" to load an existing one.
            topic_name: Human-readable project name (used when action="create").
            project_slug: Existing project slug (used when action="open").
        """
        global _doc_sm
        session = _doc_sm.session

        # Idempotent: already in doc_mode
        if session.state.value == "doc_mode":
            logger.info("[DOC] enter_doc_mode: already active (no-op)")
            await params.result_callback(
                f"ALREADY_ACTIVE: Documentation mode is already open "
                f"(project: {session.project_slug}, version: {session.version})."
            )
            return

        try:
            if action == "create":
                if not topic_name:
                    await params.result_callback(
                        "ERROR: topic_name is required when action='create'."
                    )
                    return
                project_info, version_info = create_project(topic_name)
                logger.info(
                    f"[DOC] Created project '{project_info.slug}' at {project_info.project_dir}"
                )
            elif action == "open":
                slug = project_slug or topic_name
                if not slug:
                    await params.result_callback(
                        "ERROR: project_slug is required when action='open'."
                    )
                    return
                project_info = load_project(slug)
                if project_info is None:
                    await params.result_callback(
                        f"PROJECT_NOT_FOUND: No project with slug '{slug}'."
                    )
                    return
                version_info = load_version(project_info.project_dir, project_info.current_version)
                logger.info(
                    f"[DOC] Opened project '{project_info.slug}' version {project_info.current_version}"
                )
            else:
                await params.result_callback(
                    f"ERROR: Unknown action '{action}'. Use 'create' or 'open'."
                )
                return

            _doc_sm.enter_doc_mode(
                project_slug=project_info.slug,
                version_info=version_info,
                project_dir=project_info.project_dir,
                opened_existing=(action == "open"),
            )

            # Push browser event
            from pipecat.processors.frameworks.rtvi.models import ServerMessage

            msg = ServerMessage(
                data={
                    "type": "doc-mode-entered",
                    "project_slug": project_info.slug,
                    "version": version_info.version if version_info else 0,
                }
            )
            frames = [OutputTransportMessageUrgentFrame(message=msg.model_dump())]

            # For existing projects, push the current file content to the browser overlay
            if action == "open" and version_info and version_info.document_md.exists():
                existing_content = version_info.document_md.read_text()
                if existing_content.strip():
                    content_msg = ServerMessage(
                        data={"type": "doc-content-updated", "content": existing_content}
                    )
                    frames.append(OutputTransportMessageUrgentFrame(message=content_msg.model_dump()))

            await task.queue_frames(frames)
            logger.info(f"[DOC STATE] shell → doc_mode (project={project_info.slug})")

            await params.result_callback(
                f"Documentation mode active. Project: '{project_info.display_name}', "
                f"version {version_info.version if version_info else 0}. "
                f"Files are at {version_info.version_dir if version_info else 'unknown'}."
            )

        except StateMachineError as e:
            logger.error(f"[DOC] State machine error in enter_doc_mode: {e}")
            await params.result_callback(f"{e.code}: {e.message}")
        except Exception as e:
            logger.error(f"[DOC] Unexpected error in enter_doc_mode: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def exit_doc_mode(params: FunctionCallParams, discard: bool = False):
        """Exit Documentation Mode, save the document, and restore the terminal.

        Args:
            discard: If True, exit without saving (discards the current session content).
        """
        global _doc_sm
        session = _doc_sm.session
        state = session.state.value

        if state == "shell":
            await params.result_callback("Documentation mode is not active.")
            return

        # Move into the saving state. If a previous exit attempt already left us in
        # 'saving' or 'error_recovery', resume the close instead of failing — exiting
        # must always succeed so the user is never stuck.
        if state == "doc_mode":
            try:
                _doc_sm.exit_doc_mode()  # doc_mode → saving
            except StateMachineError as e:
                logger.error(f"[DOC] State machine error in exit_doc_mode: {e}")
                await params.result_callback(f"{e.code}: {e.message}")
                return
        else:
            logger.warning(f"[DOC] exit_doc_mode resuming from stuck state '{state}'")

        if not discard and session.has_edits and session.version_info and session.doc_writer:
            try:
                vi = session.version_info
                import json

                # Read the current document.md (written incrementally by write_to_doc).
                # Strip any prior generated Summary/Transcript so re-saving an opened
                # document replaces them instead of duplicating.
                current_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
                current_doc = _strip_generated_sections(current_doc)

                # Generate a ≤5-line summary via LLM. A summary failure must never
                # abort the save — fall back to saving without one.
                try:
                    summary_text = await _generate_doc_summary(current_doc, os.getenv("OPENAI_API_KEY", ""))
                except Exception as e:
                    logger.warning(f"[DOC] Summary generation failed, saving without summary: {e}")
                    summary_text = ""

                # Build the final document:
                # 1. Summary section at top
                # 2. Existing document content (write_to_doc writes the body already)
                # 3. Transcript collapsible at the very end
                transcript_block = session.doc_writer.render_transcript_collapsible()

                if summary_text:
                    if "## Summary" in current_doc:
                        # Update the existing Summary section in place (no duplication on re-save).
                        final_doc = _replace_section(current_doc, "Summary", summary_text)
                    elif current_doc.startswith("#"):
                        # No Summary yet: insert one right after the # Title line.
                        title_end = current_doc.index("\n") + 1
                        final_doc = (
                            current_doc[:title_end]
                            + f"\n## Summary\n\n{summary_text}\n\n"
                            + current_doc[title_end:]
                        )
                    else:
                        final_doc = f"## Summary\n\n{summary_text}\n\n" + current_doc
                else:
                    final_doc = current_doc

                # Ensure transcript is always last
                final_doc = final_doc.rstrip() + "\n\n---\n\n" + transcript_block + "\n"

                atomic_write(vi.document_md, final_doc)
                atomic_write(vi.transcript_md, session.doc_writer.render_transcript_md())
                atomic_write(vi.speakers_json, json.dumps(session.speaker_map, indent=2))

                # Push updated content to browser before overlay closes
                content_msg = ServerMessage(data={"type": "doc-content-updated", "content": final_doc})
                await task.queue_frames([OutputTransportMessageUrgentFrame(message=content_msg.model_dump())])

                logger.info(
                    f"[DOC] Saved with summary + transcript collapsible "
                    f"({session.doc_writer.utterance_count()} utterances) to {vi.document_md}"
                )
            except Exception as e:
                # Never wedge: force the session back to shell so the user can re-enter.
                logger.error(f"[DOC] Save failed, force-closing doc mode: {e}")
                _doc_sm.recover_to_shell()
                await task.queue_frames([
                    OutputTransportMessageUrgentFrame(
                        message=ServerMessage(data={"type": "doc-mode-exited"}).model_dump()
                    )
                ])
                await params.result_callback(
                    f"SAVE_FAILED: {e}. Documentation mode was closed anyway — your last "
                    f"saved content is intact; re-open the project to continue."
                )
                return
        elif not discard and not session.has_edits:
            logger.info("[DOC] No edits — exiting without save")

        _doc_sm.complete_save()

        # Push browser event
        msg = ServerMessage(data={"type": "doc-mode-exited"})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info("[DOC STATE] doc_mode → shell")

        # Verify terminal is still responsive
        router = get_router()
        router.capture_output(5)

        if discard:
            action_taken = "discarded"
        elif session.has_edits:
            action_taken = "saved"
        else:
            action_taken = "closed without changes"
        await params.result_callback(
            f"Documentation mode closed. Session {action_taken}. Terminal is restored."
        )

    async def read_doc(params: FunctionCallParams):
        """Read the current contents of document.md for the active documentation session.

        Call this before making any targeted edits so you can see existing headers and content.
        """
        session = _doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        content = vi.document_md.read_text() if vi.document_md.exists() else ""
        await params.result_callback(content if content.strip() else "(Document is empty.)")

    async def write_to_doc(params: FunctionCallParams, content: str, section: str = ""):
        """Write agreed content to document.md for the active documentation session.

        Only call this after the user has confirmed the content and format.

        Args:
            content: The markdown content to write.
            section: If provided, replace only the content under this ## header.
                     If empty, content is written under the "Main Content" section,
                     leaving the title, other sections and diagrams untouched.
        """
        session = _doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            # Always merge into a section so the title, diagrams and other sections
            # are never destroyed. An empty section targets "Main Content".
            new_doc = _replace_section(current, section or "Main Content", content)
            vi = _ensure_writable_version(session)
            atomic_write(vi.document_md, new_doc)
            from pipecat.processors.frameworks.rtvi.models import ServerMessage

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(_doc_sm.session)
            logger.info(f"[DOC] write_to_doc section={section!r} ({len(new_doc)} chars)")
            await params.result_callback(
                "OK: Document updated. "
                "Now call read_doc to review the saved content, then say to the user: "
                "'This looks like a document based on the context of the conversation. "
                "Would you like to generate any associated drawings or diagrams?'"
            )
        except Exception as e:
            logger.error(f"[DOC] write_to_doc failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def edit_doc(params: FunctionCallParams, find: str, replace: str):
        """Surgically replace an exact span of text in document.md.

        Use this for any in-place change — fixing the title, correcting a word,
        rewording a sentence — instead of rewriting whole sections. The match
        must be unique: if it appears zero or multiple times, nothing is written.

        Args:
            find: The exact existing text to replace (include enough context to be unique).
            replace: The new text to put in its place.
        """
        session = _doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        if not find:
            await params.result_callback("ERROR: 'find' must not be empty.")
            return
        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            count = current.count(find)
            if count == 0:
                await params.result_callback(
                    "NOT_FOUND: That exact text is not in the document. "
                    "Call read_doc to see the current content, then retry with text copied verbatim."
                )
                return
            if count > 1:
                await params.result_callback(
                    f"AMBIGUOUS: '{find[:40]}...' appears {count} times. "
                    "Include more surrounding text so the match is unique."
                )
                return
            new_doc = current.replace(find, replace, 1)
            vi = _ensure_writable_version(session)
            atomic_write(vi.document_md, new_doc)
            from pipecat.processors.frameworks.rtvi.models import ServerMessage

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(_doc_sm.session)
            logger.info(f"[DOC] edit_doc ({len(find)} → {len(replace)} chars)")
            await params.result_callback("OK: Edit applied.")
        except Exception as e:
            logger.error(f"[DOC] edit_doc failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def insert_diagram(
        params: FunctionCallParams,
        diagram_id: str,
        diagram_type: str,
        mermaid_source: str,
        replace_placeholder: str = "",
    ):
        """Insert a new Mermaid diagram block into document.md.

        Call this when the user asks to draw or generate a diagram.
        Before calling, check MERMAID_SUPPORTED_TYPES — if the requested type is unsupported,
        tell the user and use 'flowchart' as the fallback type instead.

        Args:
            diagram_id: Unique slug for this diagram (e.g. 'auth-flow', 'class-diagram-1').
            diagram_type: Mermaid diagram keyword (e.g. 'sequenceDiagram', 'flowchart').
            mermaid_source: Complete Mermaid source starting with the diagram type keyword.
            replace_placeholder: If provided, replace the matching <!-- diagram: ... --> comment.
        """
        session = _doc_sm.session
        if session.state.value not in ("doc_mode", "diagram_focus"):
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return

        # Reject unsupported types — caller must use a supported fallback
        if diagram_type in MERMAID_UNSUPPORTED_TYPES:
            fallback_list = ", ".join(sorted(MERMAID_SUPPORTED_TYPES - {"graph"}))
            await params.result_callback(
                f"UNSUPPORTED_DIAGRAM_TYPE: '{diagram_type}' is not supported "
                f"(beta/experimental). Use one of: {fallback_list}. "
                f"Re-prompt the user with the constraint and suggest 'flowchart' as default."
            )
            return

        if diagram_type not in MERMAID_SUPPORTED_TYPES:
            await params.result_callback(
                f"UNKNOWN_DIAGRAM_TYPE: '{diagram_type}' is not a known Mermaid type."
            )
            return

        ok, err = _validate_mermaid_source(mermaid_source, diagram_type)
        if not ok:
            prev = session.last_valid_diagrams.get(diagram_id)
            await params.result_callback(
                f"INVALID_SYNTAX: {err}. "
                + (f"Previous valid source preserved." if prev else "No previous valid source.")
            )
            return

        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            new_doc = _insert_diagram_in_doc(current, diagram_id, mermaid_source, replace_placeholder or None)
            vi = _ensure_writable_version(session)
            atomic_write(vi.document_md, new_doc)
            session.last_valid_diagrams[diagram_id] = mermaid_source

            from pipecat.processors.frameworks.rtvi.models import ServerMessage
            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(_doc_sm.session)
            logger.info(f"[DOC] insert_diagram id={diagram_id} type={diagram_type}")
            await params.result_callback(
                f"OK: Diagram '{diagram_id}' inserted. "
                f"Ask the user: 'Do you want to edit diagram \"{diagram_id}\" now?' "
                f"If yes, call enter_diagram_focus(diagram_id='{diagram_id}')."
            )
        except Exception as e:
            logger.error(f"[DOC] insert_diagram failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def update_diagram(
        params: FunctionCallParams,
        diagram_id: str,
        mermaid_source: str,
    ):
        """Replace the Mermaid source of an existing diagram block in document.md.

        Call this when the user asks to change or edit an existing diagram.
        If the new source is invalid, the previous valid source is preserved.

        Args:
            diagram_id: The ID of the diagram to update (must already exist in the document).
            mermaid_source: New complete Mermaid source starting with the diagram type keyword.
        """
        session = _doc_sm.session
        if session.state.value not in ("doc_mode", "diagram_focus"):
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return

        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            existing_src = _extract_diagram_source(current, diagram_id)
            if existing_src is None:
                await params.result_callback(
                    f"ID_NOT_FOUND: No diagram with id '{diagram_id}' in document.md."
                )
                return

            # Infer diagram type from existing source for validation
            first_word = mermaid_source.strip().split()[0] if mermaid_source.strip() else ""
            ok, err = _validate_mermaid_source(mermaid_source, first_word)
            if not ok or first_word not in MERMAID_SUPPORTED_TYPES:
                prev = session.last_valid_diagrams.get(diagram_id, existing_src)
                await params.result_callback(
                    f"INVALID_SYNTAX: {err}. "
                    f"Previous valid source preserved in document."
                )
                return

            new_doc, found = _update_diagram_in_doc(current, diagram_id, mermaid_source)
            if not found:
                await params.result_callback(f"ID_NOT_FOUND: Could not locate diagram '{diagram_id}'.")
                return

            vi = _ensure_writable_version(session)
            atomic_write(vi.document_md, new_doc)
            session.last_valid_diagrams[diagram_id] = mermaid_source

            frames = []
            # Always update the doc content for when focus mode exits
            frames.append(OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
            ))
            # Also push a live re-render event if we're currently in focus mode
            if session.state.value == "diagram_focus":
                _diagram_focus_sm.begin_edit()
                _diagram_focus_sm.begin_save()
                _diagram_focus_sm.complete_save(mermaid_source)
                frames.append(OutputTransportMessageUrgentFrame(
                    message=ServerMessage(data={
                        "type": "diagram-focus-updated",
                        "diagram_id": diagram_id,
                        "mermaid_source": mermaid_source,
                    }).model_dump()
                ))
            await task.queue_frames(frames)
            _mark_doc_session_edited(_doc_sm.session)
            logger.info(f"[DOC] update_diagram id={diagram_id} ({len(mermaid_source)} chars)")
            await params.result_callback(f"OK: Diagram '{diagram_id}' updated.")
        except Exception as e:
            logger.error(f"[DOC] update_diagram failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def move_diagram(params: FunctionCallParams, diagram_id: str, target_section: str):
        """Move an existing diagram under a different section, without duplicating it.

        Use this when the user asks to move/relocate a diagram (e.g. 'put the diagram
        under the Fuel Considerations section'). Do NOT use write_to_doc to relocate a
        diagram — that leaves the original behind. The diagram keeps its id.

        Args:
            diagram_id: The id of the diagram to move (must already exist in the document).
            target_section: The exact header text to move it under (any level, e.g.
                'Fuel Considerations for Light Ships'). Call read_doc first to get it right.
        """
        session = _doc_sm.session
        if session.state.value not in ("doc_mode", "diagram_focus"):
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            new_doc, status = _move_diagram_in_doc(current, diagram_id, target_section)
            if status == "no_diagram":
                await params.result_callback(
                    f"ID_NOT_FOUND: No diagram with id '{diagram_id}' in the document."
                )
                return
            if status == "no_section":
                await params.result_callback(
                    f"SECTION_NOT_FOUND: No section titled '{target_section}'. "
                    "Call read_doc to see the exact header text, then retry."
                )
                return

            vi = _ensure_writable_version(session)
            atomic_write(vi.document_md, new_doc)

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(_doc_sm.session)
            logger.info(f"[DOC] move_diagram id={diagram_id} → section '{target_section}'")
            await params.result_callback(
                f"OK: Diagram '{diagram_id}' moved under '{target_section}'."
            )
        except Exception as e:
            logger.error(f"[DOC] move_diagram failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def enter_diagram_focus(params: FunctionCallParams, diagram_id: str):
        """Enter Diagram Focus Mode for a specific diagram.

        Hides all other UI and shows only the diagram fullscreen.
        In Focus Mode the controller only handles diagram edits — no terminal commands.
        Call this when the user asks to edit or view a specific diagram.

        Args:
            diagram_id: The diagram to focus on (must exist in document.md).
        """
        global _diagram_focus_sm
        session = _doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback(
                f"INVALID_STATE: enter_diagram_focus requires doc_mode "
                f"(current: {session.state.value})."
            )
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return

        current = vi.document_md.read_text() if vi.document_md.exists() else ""
        src = _extract_diagram_source(current, diagram_id)
        if src is None:
            await params.result_callback(
                f"ID_NOT_FOUND: No diagram with id '{diagram_id}' in document.md."
            )
            return

        try:
            _doc_sm.enter_diagram_focus(diagram_id)
            _diagram_focus_sm.enter(diagram_id, src)
        except (StateMachineError, Exception) as e:
            await params.result_callback(f"ERROR: {e}")
            return

        # Inject scoped system prompt — narrows the controller to diagram-only commands
        context.add_message({
            "role": "user",
            "content": (
                "[SYSTEM NOTE — DIAGRAM FOCUS MODE ACTIVE]\n"
                f"You are now in Diagram Focus Mode for diagram '{diagram_id}'.\n"
                "RULES while in this mode:\n"
                "1. Only respond to diagram-related requests (describe changes, update source, exit).\n"
                "2. If the user asks to do something unrelated (run a command, write to doc, etc.), "
                "politely say you can only handle diagram edits right now and ask them to exit first.\n"
                "3. When the user describes a change, rewrite the Mermaid source and call update_diagram.\n"
                "4. When the user says 'exit diagram mode', 'done', or 'save and exit', call exit_diagram_focus().\n"
                "5. Keep replies short — the user is looking at the diagram, not reading text."
            ),
        })

        msg = ServerMessage(data={
            "type": "diagram-focus-entered",
            "diagram_id": diagram_id,
            "mermaid_source": src,
        })
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[DIAGRAM FOCUS] doc_mode → diagram_focus (id={diagram_id}, inner={_diagram_focus_sm.state})")
        await params.result_callback(
            f"OK: Diagram Focus Mode active for '{diagram_id}'. "
            f"Describe changes to update the diagram, or say 'exit diagram mode' when done."
        )

    async def exit_diagram_focus(params: FunctionCallParams):
        """Exit Diagram Focus Mode and return to the documentation view.

        Call this when the user says 'exit diagram mode', 'done', or 'save and exit'.
        """
        global _diagram_focus_sm
        session = _doc_sm.session
        if session.state.value != "diagram_focus":
            await params.result_callback(
                f"INVALID_STATE: exit_diagram_focus requires diagram_focus "
                f"(current: {session.state.value})."
            )
            return

        diagram_id = _diagram_focus_sm.session.diagram_id or session.active_diagram_id
        try:
            _doc_sm.exit_diagram_focus()
            _diagram_focus_sm.exit()
        except (StateMachineError, Exception) as e:
            await params.result_callback(f"ERROR: {e}")
            return

        msg = ServerMessage(data={"type": "diagram-focus-exited", "diagram_id": diagram_id})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[DIAGRAM FOCUS] diagram_focus → doc_mode (id={diagram_id})")
        await params.result_callback(
            f"OK: Diagram Focus Mode exited. Documentation view restored."
        )

    async def _revert_diagram_edit() -> tuple[str, str]:
        """Shared rollback: restore the diagram's previous source and re-render it.

        Returns (status, message) where status is "ok", "not_in_focus", "no_version",
        "nothing_to_revert", or "error". Used by both the revert_diagram_edit tool and
        the browser's render-failure auto-revert. The re-pushed diagram-focus-updated
        carries reverted=True so a still-broken render won't trigger another revert.
        """
        global _diagram_focus_sm
        session = _doc_sm.session
        if session.state.value != "diagram_focus":
            return "not_in_focus", "Not in diagram focus mode."
        vi = session.version_info
        if vi is None:
            return "no_version", "No version info in current session."

        fs = _diagram_focus_sm.session
        diagram_id = fs.diagram_id or session.active_diagram_id
        prev_source = _diagram_focus_sm.revert()
        if prev_source is None:
            return "nothing_to_revert", "There's no earlier version to go back to."

        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            new_doc, found = _update_diagram_in_doc(current, diagram_id, prev_source)
            if found:
                atomic_write(vi.document_md, new_doc)
            session.last_valid_diagrams[diagram_id] = prev_source

            await task.queue_frames([
                OutputTransportMessageUrgentFrame(
                    message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
                ),
                OutputTransportMessageUrgentFrame(
                    message=ServerMessage(data={
                        "type": "diagram-focus-updated",
                        "diagram_id": diagram_id,
                        "mermaid_source": prev_source,
                        "reverted": True,
                    }).model_dump()
                ),
            ])
            logger.info(f"[DIAGRAM FOCUS] Reverted edit for id={diagram_id}")
            return "ok", f"Rolled back to the previous version of '{diagram_id}'."
        except Exception as e:
            logger.error(f"[DIAGRAM FOCUS] revert failed: {e}")
            return "error", str(e)

    async def revert_diagram_edit(params: FunctionCallParams):
        """Roll back the most recent diagram edit, restoring the previous version.

        Call this in Diagram Focus Mode when the user says the change was not okay
        ('no', 'undo', 'revert', 'go back'). Restores the diagram as it was before the
        last update_diagram and re-renders it. Single-level undo.
        """
        status, message = await _revert_diagram_edit()
        if status == "not_in_focus":
            await params.result_callback(f"INVALID_STATE: revert_diagram_edit requires diagram_focus.")
        elif status == "no_version":
            await params.result_callback(f"ERROR: {message}")
        elif status == "nothing_to_revert":
            await params.result_callback(f"NOTHING_TO_REVERT: {message} Describe the change you want instead.")
        elif status == "error":
            await params.result_callback(f"WRITE_ERROR: {message}")
        else:
            await params.result_callback(
                "OK: Rolled back to the previous version of the diagram. "
                "Describe another change, or say 'exit diagram mode' when done."
            )

    async def search_images(params: FunctionCallParams, query: str, element_id: str):
        """Search for images to embed in the current diagram node.

        Call this when the user asks to replace a diagram element with an image.
        Downloads up to 5 images locally and sends them to the browser as thumbnails.

        Args:
            query: Search query, e.g. "AWS S3 bucket icon transparent PNG"
            element_id: The Mermaid node ID to replace, e.g. "DB" or "User"
        """
        global _diagram_focus_sm
        session = _doc_sm.session
        if session.state.value != "diagram_focus":
            await params.result_callback("INVALID_STATE: Not in diagram focus mode.")
            return
        if _diagram_focus_sm.session.in_image_search:
            await params.result_callback("ALREADY_ACTIVE: Image search already in progress. Say cancel to start over.")
            return

        search_dir = _image_tmp_root / uuid.uuid4().hex[:8]
        search_dir.mkdir(parents=True, exist_ok=True)
        _diagram_focus_sm.begin_image_search(element_id, search_dir)

        # Run DuckDuckGo search in thread pool (synchronous library)
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: _ddg_image_search(query, 5))
        except Exception as e:
            _diagram_focus_sm.cancel_image_search()
            await params.result_callback(f"SEARCH_ERROR: {e}")
            return

        if not results:
            _diagram_focus_sm.cancel_image_search()
            await params.result_callback("NO_RESULTS: No images found. Try a different description.")
            return

        # Download all images concurrently
        downloaded: list[Path] = []
        async with aiohttp.ClientSession() as http:
            tasks = [_download_image(http, r["image"], search_dir / f"{i+1}", i+1)
                     for i, r in enumerate(results)]
            paths = await asyncio.gather(*tasks, return_exceptions=True)

        for p in paths:
            if isinstance(p, Path):
                downloaded.append(p)

        if not downloaded:
            _diagram_focus_sm.cancel_image_search()
            await params.result_callback("DOWNLOAD_ERROR: Could not download any images. Try again.")
            return

        _diagram_focus_sm.set_image_results(downloaded)

        # Tell browser to show thumbnails
        thumb_list = [
            {"n": i + 1, "url": f"/api/images/{_session_id}/{p.name}"}
            for i, p in enumerate(downloaded)
        ]
        msg = ServerMessage(data={
            "type": "image-search-results",
            "element_id": element_id,
            "images": thumb_list,
        })
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[IMAGE] Search '{query}' → {len(downloaded)} images for element '{element_id}'")
        await params.result_callback(
            f"OK: Found {len(downloaded)} images. Thumbnails shown to user. "
            f"Ask: 'Which image would you like — 1 through {len(downloaded)}? Say cancel to go back.'"
        )

    async def select_image(params: FunctionCallParams, number: int):
        """Select one of the image search results to embed in the diagram.

        Call this when the user names a number. Deletes the other images,
        embeds the chosen one in the Mermaid node, and enters sizing state.

        Args:
            number: 1-based index of the chosen image (1–5)
        """
        global _diagram_focus_sm
        fs = _diagram_focus_sm.session
        if fs.image_search_state != "selecting":
            await params.result_callback("INVALID_STATE: Not in image selection state.")
            return

        idx = number - 1
        if idx < 0 or idx >= len(fs.image_paths):
            await params.result_callback(f"INVALID: Please pick a number between 1 and {len(fs.image_paths)}.")
            return

        chosen = fs.image_paths[idx]

        # Delete the unchosen temp images immediately
        for i, p in enumerate(fs.image_paths):
            if i != idx:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

        # Copy chosen image to permanent version directory
        doc_session = _doc_sm.session
        vi = doc_session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info.")
            return

        # Validate the target diagram block and node BEFORE forking or copying,
        # so a failed embed never creates an orphan version.
        base_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
        if _extract_diagram_source(base_doc, fs.diagram_id) is None:
            await params.result_callback(
                f"ID_NOT_FOUND: Could not locate diagram '{fs.diagram_id}' in document.md."
            )
            return
        _, node_found = _embed_image_in_node(
            fs.current_source or "", fs.image_search_element_id, "about:blank", fs.current_image_width
        )
        if not node_found:
            await params.result_callback(
                f"NODE_NOT_FOUND: Could not find node '{fs.image_search_element_id}' in Mermaid source. "
                f"Try update_diagram manually."
            )
            return

        # All checks passed — fork (if needed) so the image and URL land in the writable version.
        vi = _ensure_writable_version(doc_session)

        images_dir = vi.version_dir / "images"
        images_dir.mkdir(exist_ok=True)
        dest_name = f"{fs.diagram_id}-{fs.image_search_element_id}{chosen.suffix}"
        dest = images_dir / dest_name
        shutil.copy2(chosen, dest)

        # Transition state machine
        _diagram_focus_sm.select_image(dest)

        # Embed image in Mermaid source (URL points at the writable version)
        img_url = f"/api/docs/{doc_session.project_slug}/version/{doc_session.version}/images/{dest_name}"
        new_source, _ = _embed_image_in_node(
            fs.current_source or "", fs.image_search_element_id, img_url, fs.current_image_width
        )

        current_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
        new_doc, _ = _update_diagram_in_doc(current_doc, fs.diagram_id, new_source)

        atomic_write(vi.document_md, new_doc)
        _mark_doc_session_edited(doc_session)
        _diagram_focus_sm.session.current_source = new_source

        # Send live re-render
        frames = [
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
            ),
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={
                    "type": "diagram-focus-updated",
                    "diagram_id": fs.diagram_id,
                    "mermaid_source": new_source,
                }).model_dump()
            ),
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "image-search-clear"}).model_dump()
            ),
        ]
        await task.queue_frames(frames)
        logger.info(f"[IMAGE] Selected image {number} → {dest_name}, width={fs.current_image_width}px")
        await params.result_callback(
            f"OK: Image embedded at {fs.current_image_width}px wide. "
            f"Ask: 'How does that look? Say bigger, smaller, or done.'"
        )

    async def resize_image(params: FunctionCallParams, direction: str):
        """Adjust the width of the embedded image. Call during sizing state.

        Args:
            direction: 'bigger' or 'smaller'
        """
        global _diagram_focus_sm
        fs = _diagram_focus_sm.session
        if fs.image_search_state != "sizing":
            await params.result_callback("INVALID_STATE: Not in image sizing state.")
            return

        if direction.lower() in ("bigger", "larger", "up"):
            new_width = _diagram_focus_sm.width_bigger()
        elif direction.lower() in ("smaller", "smaller", "down"):
            new_width = _diagram_focus_sm.width_smaller()
        else:
            await params.result_callback("INVALID: Say 'bigger' or 'smaller'.")
            return

        doc_session = _doc_sm.session
        vi = doc_session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info.")
            return
        if fs.selected_image_path is None:
            await params.result_callback("ERROR: No selected image to resize.")
            return

        img_url = f"/api/docs/{doc_session.project_slug}/version/{doc_session.version}/images/{fs.selected_image_path.name}"
        new_source, _ = _embed_image_in_node(
            fs.current_source or "", fs.image_search_element_id, img_url, new_width
        )
        current_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
        new_doc, doc_found = _update_diagram_in_doc(current_doc, fs.diagram_id, new_source)
        if not doc_found:
            await params.result_callback(
                f"ID_NOT_FOUND: Could not locate diagram '{fs.diagram_id}' in document.md."
            )
            return

        atomic_write(vi.document_md, new_doc)
        _mark_doc_session_edited(doc_session)
        _diagram_focus_sm.session.current_source = new_source

        frames = [
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
            ),
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={
                    "type": "diagram-focus-updated",
                    "diagram_id": fs.diagram_id,
                    "mermaid_source": new_source,
                }).model_dump()
            ),
        ]
        await task.queue_frames(frames)
        logger.info(f"[IMAGE] Resized to {new_width}px")
        await params.result_callback(f"OK: Image is now {new_width}px wide. Bigger, smaller, or done?")

    async def cancel_image_search(params: FunctionCallParams):
        """Cancel image search and return to diagram editing without embedding anything."""
        global _diagram_focus_sm
        if not _diagram_focus_sm.session.in_image_search:
            await params.result_callback("NOT_ACTIVE: No image search in progress.")
            return
        _diagram_focus_sm.cancel_image_search()
        msg = ServerMessage(data={"type": "image-search-clear"})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info("[IMAGE] Search cancelled, temp files deleted")
        await params.result_callback("OK: Image search cancelled. Back to diagram editing.")

    async def done_image(params: FunctionCallParams):
        """Confirm the embedded image size and exit image search state.

        Call when the user says 'done', 'looks good', 'that's fine', etc.
        """
        global _diagram_focus_sm
        if _diagram_focus_sm.session.image_search_state != "sizing":
            await params.result_callback("INVALID_STATE: Not in image sizing state.")
            return
        _diagram_focus_sm.complete_image_search()
        logger.info("[IMAGE] Image embedding confirmed, returning to diagram editing")
        await params.result_callback("OK: Image confirmed. Back to diagram editing. Any other changes?")

    async def web_search(params: FunctionCallParams, query: str, max_results: int = 5):
        """Search the web for current or factual information and return the top results.

        Call this whenever answering needs up-to-date facts, recent events, specific
        figures, or anything you are not confident about from memory. Base your spoken
        answer on the returned results and mention the source.

        Args:
            query: The search query.
            max_results: How many results to return (1–8, default 5).
        """
        q = (query or "").strip()
        if not q:
            await params.result_callback("ERROR: query must not be empty.")
            return
        n = max(1, min(8, int(max_results) if max_results else 5))
        results, ok, failed = await _multi_search(q, n)

        if not results:
            detail = f" (all backends failed: {', '.join(failed)})" if failed else ""
            await params.result_callback(
                f"NO_RESULTS for '{q}'{detail}. Tell the user you couldn't find anything online "
                "and answer from your own knowledge if you can."
            )
            return

        backends_note = f"backends used: {', '.join(ok)}" + (
            f"; unavailable: {', '.join(failed)}" if failed else ""
        )
        lines = [f"Search results for '{q}' ({backends_note}):", ""]
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            srcs = ", ".join(r.get("sources", []))
            lines.append(f"{i}. {title}\n   {body}\n   Source: {href}  [via {srcs}]")
        logger.info(f"[WEB] web_search '{q}' → {len(results)} merged results (ok={ok} failed={failed})")
        await params.result_callback(
            "\n".join(lines)
            + "\n\nSummarize the answer for the user from these results and cite the source(s). "
            "Results returned by more than one backend are more trustworthy. "
            "If you need the full text of one result, call fetch_url with its Source link."
        )

    async def fetch_url(params: FunctionCallParams, url: str):
        """Fetch the readable text of a web page (e.g. a web_search result) for a deeper answer.

        Args:
            url: The page URL to fetch (use a Source link from web_search).
        """
        u = (url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            await params.result_callback("ERROR: url must start with http:// or https://")
            return
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(u, headers={"User-Agent": "Mozilla/5.0 (cockpit)"}) as resp:
                    if resp.status != 200:
                        await params.result_callback(f"FETCH_ERROR: HTTP {resp.status} for {u}")
                        return
                    html = await resp.text()
        except Exception as e:
            logger.error(f"[WEB] fetch_url failed: {e}")
            await params.result_callback(f"FETCH_ERROR: {e}")
            return
        # Strip tags/scripts to rough plain text and cap length for the context window.
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 6000:
            text = text[:6000] + " …[truncated]"
        logger.info(f"[WEB] fetch_url {u} → {len(text)} chars")
        await params.result_callback(
            f"Readable text from {u}:\n\n{text}\n\nAnswer the user's question from this and cite the source."
        )

    async def set_speaker_name(params: FunctionCallParams, speaker_id: str, name: str):
        """Assign a human-readable name to a Deepgram speaker ID in the active doc session.

        Call this after asking the user who an unrecognised speaker is.
        All past and future utterances from that speaker ID will use this name.

        Args:
            speaker_id: The Deepgram speaker ID string, e.g. "1", "2".
            name: The human-readable name to assign, e.g. "Alice".
        """
        session = _doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        sid = str(speaker_id)
        session.speaker_map[sid] = name
        if session.doc_writer:
            session.doc_writer.set_speaker_map(session.speaker_map)
        # Persist the updated map immediately so it survives a crash
        if session.version_info:
            import json as _json
            atomic_write(
                session.version_info.version_dir / "speakers.json",
                _json.dumps(session.speaker_map, indent=2),
            )
        logger.info(f"[SPEAKER] id={sid} → '{name}'")

        # Notify the speaker gate that this speaker has been identified
        await speaker_gate.mark_speaker_identified(sid)

        await params.result_callback(f"OK: Speaker {sid} is now labelled '{name}' in the transcript.")

    for _svc in (llm_openai, llm_anthropic, llm_ollama):
        _svc.register_direct_function(run_command)
        _svc.register_direct_function(send_input)
        _svc.register_direct_function(capture_output)
        _svc.register_direct_function(find_directory)
        _svc.register_direct_function(list_doc_projects)
        _svc.register_direct_function(enter_doc_mode)
        _svc.register_direct_function(exit_doc_mode)
        _svc.register_direct_function(read_doc)
        _svc.register_direct_function(write_to_doc)
        _svc.register_direct_function(edit_doc)
        _svc.register_direct_function(insert_diagram)
        _svc.register_direct_function(update_diagram)
        _svc.register_direct_function(move_diagram)
        _svc.register_direct_function(enter_diagram_focus)
        _svc.register_direct_function(exit_diagram_focus)
        _svc.register_direct_function(revert_diagram_edit)
        _svc.register_direct_function(search_images)
        _svc.register_direct_function(select_image)
        _svc.register_direct_function(resize_image)
        _svc.register_direct_function(cancel_image_search)
        _svc.register_direct_function(done_image)
        _svc.register_direct_function(web_search)
        _svc.register_direct_function(fetch_url)
        _svc.register_direct_function(set_speaker_name)

    tools = ToolsSchema(
        [
            run_command,
            send_input,
            capture_output,
            find_directory,
            list_doc_projects,
            enter_doc_mode,
            exit_doc_mode,
            read_doc,
            write_to_doc,
            edit_doc,
            insert_diagram,
            update_diagram,
            move_diagram,
            enter_diagram_focus,
            exit_diagram_focus,
            revert_diagram_edit,
            search_images,
            select_image,
            resize_image,
            cancel_image_search,
            done_image,
            web_search,
            fetch_url,
            set_speaker_name,
        ]
    )

    system_prompt = (
        "You are the controller for a local voice coding cockpit. "
        "The terminal on the right is a single fish shell running inside a tmux session.\n\n"
        "TOOLS:\n"
        "- run_command(command, directory_path): run a shell command in the terminal\n"
        "- send_input(text): send text to whatever is currently running in the terminal\n"
        "- capture_output(lines): read recent output from the terminal\n"
        "- find_directory(name): find a directory by partial name\n"
        "- list_doc_projects(): list all existing documentation projects (slugs + names)\n"
        "- enter_doc_mode(action, topic_name, project_slug): enter Documentation Mode\n"
        "  - action='create' + topic_name: start a new documentation project\n"
        "  - action='open' + project_slug: open an existing project\n"
        "- exit_doc_mode(discard): save and close Documentation Mode\n"
        "- read_doc(): read the current document.md — call this before any targeted edit\n"
        "- write_to_doc(content, section): write agreed content to the document\n"
        "  - section empty: write under the 'Main Content' section (title/diagrams/other sections are preserved)\n"
        "  - section='Header Name': replace only the content under that ## header\n"
        "  - write_to_doc NEVER erases existing text or diagrams; to add a diagram use insert_diagram\n"
        "  - write_to_doc is for adding/replacing a whole ## section — NOT for fixing the title or a word\n"
        "- edit_doc(find, replace): surgically replace an EXACT span of text in the document\n"
        "  - use this for any in-place change: fixing the # title, correcting a word, rewording a sentence\n"
        "  - 'find' must match the document verbatim and be unique; call read_doc first to copy it exactly\n"
        "  - if it reports NOT_FOUND or AMBIGUOUS, read_doc again and retry with more surrounding context\n"
        "- insert_diagram(diagram_id, diagram_type, mermaid_source, replace_placeholder): add a new Mermaid diagram\n"
        "  - diagram_id: unique slug, e.g. 'auth-flow' or 'class-diagram-1'\n"
        "  - diagram_type: Mermaid keyword (sequenceDiagram, flowchart, classDiagram, etc.)\n"
        "  - mermaid_source: full Mermaid source starting with the diagram type keyword\n"
        "  - replace_placeholder: description text from a <!-- diagram: ... --> comment to replace\n"
        "  - UNSUPPORTED TYPES: xychart-beta, sankey-beta, C4Context — if requested, notify the user and use 'flowchart' instead\n"
        "- update_diagram(diagram_id, mermaid_source): replace the source of an existing diagram\n"
        "- move_diagram(diagram_id, target_section): move an existing diagram under another section\n"
        "  - ALWAYS use this to relocate a diagram; NEVER use write_to_doc to move one (that duplicates it)\n"
        "  - call read_doc first to copy the exact target header text\n"
        "- enter_diagram_focus(diagram_id): enter Focus Mode — hides terminal and doc view, shows diagram fullscreen\n"
        "- exit_diagram_focus(): exit Focus Mode and restore the documentation view\n"
        "- revert_diagram_edit(): undo the last update_diagram, restoring the previous version (use when the user rejects an edit)\n"
        "- search_images(query, element_id): search for images to embed in a diagram node\n"
        "- select_image(number): select one of the search results (1–5) to embed\n"
        "- resize_image(direction): adjust embedded image size; direction is 'bigger' or 'smaller'\n"
        "- done_image(): confirm the image size and exit image search state\n"
        "- cancel_image_search(): cancel image search without embedding anything\n"
        "- web_search(query, max_results): search the web for current/factual info; base your answer on the results\n"
        "- fetch_url(url): fetch the readable text of a page (use a Source link from web_search) for a deeper answer\n"
        "- set_speaker_name(speaker_id, name): assign a name to a Deepgram speaker ID in the active doc session\n\n"
        "WORKFLOW:\n"
        "1. When the user names a directory, use find_directory first to confirm the full path.\n"
        "2. Confirm with the user before proceeding if the match isn't exact.\n"
        "3. After running a command, capture_output and summarize what happened.\n"
        "4. directory_path in run_command: omit it (leave empty) for commands that should run in the current working\n"
        "   directory — e.g. pwd, ls, git status, cloud, claude, or any REPL or interactive tool.\n"
        "   Only supply directory_path when the user explicitly names a different directory to work in.\n"
        "   Fish shell abbreviates long paths in the prompt (e.g. ~/s/p/p/server means ~/src/pipecat/phone-coder/server);\n"
        "   never infer a directory_path from the prompt display.\n"
        "4. Documentation Mode rules:\n"
        "   - ENTER: trigger only on the exact phrase 'enter documentation mode'.\n"
        "     * If the user says 'enter documentation mode for <name>', call enter_doc_mode(action='create', topic_name='<name>') immediately.\n"
        "     * Otherwise ask: 'Do you want to open an existing document or create a new one?'\n"
        "       - 'create' → ask for a project name, then call enter_doc_mode(action='create', topic_name=<name>).\n"
        "       - 'open'   → call list_doc_projects() first to show available projects, then ask the user which one, then call enter_doc_mode(action='open', project_slug=<slug>).\n"
        "   - EXIT: trigger only on the exact phrase 'exit documentation mode'.\n"
        "     * Call exit_doc_mode(). If the user adds 'and discard', call exit_doc_mode(discard=True).\n"
        "   - Do NOT call enter_doc_mode or exit_doc_mode for any other phrasing.\n"
        "   - WRITING: only call write_to_doc after the user has explicitly confirmed the content.\n"
        "     Before editing a specific section, always call read_doc first.\n"
        "   - DOCUMENT STRUCTURE: keep the document split into small, logically-titled ## sections so\n"
        "     each can be edited independently. Required layout:\n"
        "       * '## Summary' — first section, a brief overview (auto-maintained on exit; you may also write it).\n"
        "       * One '## <Topic>' section per distinct subject, in the order discussed.\n"
        "       * Use '### <Subtopic>' headers within a topic when it has sub-parts.\n"
        "       * '## Action Items' then '## Open Issues' — always the last two sections.\n"
        "     SIZING: aim for each ## topic to be roughly 10–30 lines. If a topic grows past ~30 lines,\n"
        "     split it into a new ## topic or add ### subtopics. If a topic is under ~10 lines, fold it\n"
        "     into a related topic rather than leaving a tiny standalone section.\n"
        "   - EDITING EXISTING TEXT: for a small change (the title, a word, a phrase), call read_doc, then\n"
        "     edit_doc(find=<verbatim existing text>, replace=<new text>). NEVER rewrite the whole document\n"
        "     through write_to_doc to make a small edit — that nests content in the wrong place.\n"
        "     To change the # title 'Old' to 'New', call edit_doc(find='# Old', replace='# New').\n"
        "     Only use write_to_doc(content, section='<exact ## header>') when replacing an entire section's body.\n"
        "   - SPEAKER NAMING: when you receive a SYSTEM NOTE about a new speaker ID, ask the user who that\n"
        "     person is (e.g. 'I heard a new voice — who is that?'), then call set_speaker_name with their answer.\n"
        "     Only ask once per speaker ID. Do not ask again if already named.\n"
        "   - DIAGRAMS (Phase 2):\n"
        "     1. After write_to_doc, ask: 'Would you like to generate any diagrams for this document?'\n"
        "     2. CONFIRM BEFORE GENERATING: never call insert_diagram until the user has explicitly approved\n"
        "        the content. First state, in one or two sentences, the exact text/points the diagram will be\n"
        "        based on (e.g. 'I'll draw a flowchart of: user signs in → token issued → dashboard loads. Shall I?').\n"
        "        Only if the user answers 'yes' (or equivalent) do you call insert_diagram. If they say no or\n"
        "        suggest changes, revise the description and ask again — do NOT generate until you hear yes.\n"
        "        This applies especially to a brand-new document: confirm the source text first, then generate.\n"
        "     3. Once approved, call insert_diagram with the appropriate diagram_type and mermaid_source.\n"
        "        Generate real Mermaid syntax — not placeholders.\n"
        "     4. After insert_diagram succeeds, ask: 'Do you want to edit diagram \"<id>\" now?'\n"
        "        - If yes: call enter_diagram_focus(diagram_id='<id>')\n"
        "        - If no: continue to the next diagram or finish\n"
        "     5. In Focus Mode: the user describes a change → call update_diagram → then ASK 'Does that look right?'\n"
        "        - If the user says yes: wait for the next change or 'exit diagram mode'.\n"
        "        - If the user says no / 'undo' / 'revert' / 'go back': call revert_diagram_edit() to restore the\n"
        "          previous version, confirm it's rolled back, and ask what they'd like instead. Stay in Focus Mode.\n"
        "        When the user is done, call exit_diagram_focus().\n"
        "     6. UNSUPPORTED TYPE: if the user requests xychart-beta, sankey-beta, or C4Context,\n"
        "        say: 'That diagram type is in beta and not supported. I'll use a flowchart instead.'\n"
        "        Then call insert_diagram with diagram_type='flowchart'.\n"
        "     7. DIAGRAM IDs: use descriptive kebab-case slugs. For multiple diagrams in one session,\n"
        "        use distinct IDs (e.g. 'auth-flow', 'class-diagram-users'). Never reuse an existing ID.\n"
        "   - IMAGE EMBEDDING (in Focus Mode only):\n"
        "     1. Trigger: user says 'replace X with an image', 'use an icon for X', or similar.\n"
        "        Identify the Mermaid node ID (element_id) from the current source, then call search_images.\n"
        "     2. After thumbnails appear in browser, speak only: 'I found N images. Which would you like,\n"
        "        1 through N? Say cancel to go back.' Do NOT describe each image by voice.\n"
        "     3. On user picking a number: call select_image(number). Image embeds at default 40px.\n"
        "     4. Ask: 'How does that look? Say bigger, smaller, or done.'\n"
        "     5. On 'bigger'/'smaller': call resize_image(direction). Repeat step 4.\n"
        "     6. On 'done'/'looks good'/'that's fine': call done_image().\n"
        "     7. On 'cancel' at any point during image search: call cancel_image_search().\n"
        "     8. While in image search state, refuse all unrelated requests.\n\n"
        "WEB SEARCH:\n"
        "- When a question needs current events, recent facts, specific numbers, or anything you're not "
        "confident about from memory, call web_search FIRST, then answer from the results and cite the source.\n"
        "- Don't search for things you already know or for opinions/chit-chat. Keep spoken answers brief.\n"
        "- Use fetch_url only when the search snippets aren't enough and you need a page's full text.\n\n"
        "OUTPUT STYLE:\n"
        "- Watch for [SYSTEM NOTE] messages about voice output being ON or OFF.\n"
        "- When voice is ON, your reply is spoken aloud: keep it to 1–3 short, conversational sentences.\n"
        "  Never recite long outlines, numbered headers, or bullet lists aloud — write that content to the\n"
        "  document or give a one-line spoken summary and ask if they want it written down.\n"
        "- When voice is OFF, you may use full Markdown and longer, detailed replies.\n\n"
        "SAFETY:\n"
        "1. Never run destructive commands (rm -rf, git reset --hard, etc.) without explicit confirmation.\n"
        "2. Do not auto-commit unless asked.\n"
        "3. Be concise and direct in your replies."
    )

    context = LLMContext(messages=[{"role": "system", "content": system_prompt}], tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    speaker_gate = SpeakerIdentificationGate()
    printer = CockpitPrinter(speaker_gate=speaker_gate)
    tts_gate = TTSGate(tts_state)

    # Pipeline
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            speaker_gate,
            inspector,
            llm,
            printer,
            tts_gate,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    speaker_gate.set_task_context(task, context)
    printer.set_task_context(task, context)

    async def send_tts_status(reason: str = ""):
        msg = ServerMessage(
            data={
                "type": "tts-status",
                "enabled": tts_state.enabled,
                "provider": tts_state.provider,
                "reason": reason,
            }
        )
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])

    async def send_model_status():
        msg = ServerMessage(data={"type": "model-status", "model": model_state.model})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await send_tts_status("Text-only mode is active.")
        await send_model_status()
        # Kick off the conversation
        context.add_message(
            {
                "role": "user",
                "content": "Please introduce yourself as the Voice Coding Cockpit controller.",
            }
        )
        await task.queue_frames([LLMRunFrame()])

    @task.rtvi.event_handler("on_client_message")
    async def on_client_message(rtvi, message):
        data = message.data or {}
        if message.type == "tts-toggle":
            enabled = bool(data.get("enabled"))
            tts_state.set_enabled(enabled)
            logger.info(f"TTS {'enabled' if enabled else 'disabled'} by browser toggle")
            # Remind the controller of the current output channel so it adjusts verbosity.
            # (No LLMRunFrame — this just informs the next real turn, no unsolicited reply.)
            if enabled:
                context.add_message({
                    "role": "user",
                    "content": (
                        "[SYSTEM NOTE] Voice output is now ON — your replies are spoken aloud. "
                        "Keep them short and conversational: 1–3 sentences. Do NOT read out long "
                        "Markdown structures, headers, or bullet lists. When you want to propose a "
                        "document outline or detailed content, write it to the document (or briefly "
                        "summarize it in speech) instead of reciting it."
                    ),
                })
            else:
                context.add_message({
                    "role": "user",
                    "content": (
                        "[SYSTEM NOTE] Voice output is now OFF — replies are shown as text. "
                        "You may use full Markdown formatting and longer, detailed responses."
                    ),
                })
            await send_tts_status("Voice enabled." if enabled else "Text-only mode is active.")
        elif message.type == "diagram-render-failed":
            # The browser couldn't render the last diagram edit — roll it back automatically.
            status, _msg = await _revert_diagram_edit()
            logger.info(f"[DIAGRAM FOCUS] Auto-revert after render failure: {status}")
            if status == "ok":
                # Let the controller know so it can mention it on the next turn.
                context.add_message({
                    "role": "user",
                    "content": (
                        "[SYSTEM NOTE] The last diagram change produced invalid Mermaid that wouldn't "
                        "render, so it was automatically reverted to the previous version. Tell the user "
                        "briefly and ask them to describe the change differently."
                    ),
                })
        elif message.type == "tts-provider":
            provider = data.get("provider", "cartesia")
            if provider not in _tts_services:
                return
            tts_state.provider = provider
            service = _tts_services[provider]
            await task.queue_frames([ManuallySwitchServiceFrame(service=service)])
            logger.info(f"TTS provider switched to {provider}")
            await send_tts_status(f"Switched to {provider.capitalize()} TTS.")
        elif message.type == "model-switch":
            new_model = data.get("model", "")
            if new_model not in _MODEL_PRICING:
                logger.warning(f"Unknown model requested: {new_model}")
                return
            old_provider = model_state.provider
            model_state.model = new_model
            new_provider = model_state.provider
            target_svc = _llm_by_provider[new_provider]
            target_svc.set_full_model_name(new_model)

            if old_provider != new_provider:
                # Cross-provider: reset context to avoid message-format incompatibility
                context.messages = [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": "The user switched AI models mid-conversation. Continue naturally.",
                    },
                ]
                await task.queue_frames([ManuallySwitchServiceFrame(service=target_svc)])
                logger.info(
                    f"LLM switched to {new_model} (provider {old_provider}→{new_provider}, context reset)"
                )
            else:
                logger.info(f"LLM model updated to {new_model} (same provider {new_provider})")

            await send_model_status()

    async def _keepalive_loop():
        """Send a small ping every 25s to keep the WebRTC ICE connection alive."""
        ping = ServerMessage(data={"type": "ping"})
        frame = OutputTransportMessageUrgentFrame(message=ping.model_dump())
        while True:
            await asyncio.sleep(25)
            try:
                await task.queue_frames([frame])
            except Exception:
                break

    _keepalive_task: asyncio.Task | None = None

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal _keepalive_task
        logger.info("Client connected — starting tmux + ttyd")
        ensure_terminal_running(ttyd_port)
        _keepalive_task = asyncio.ensure_future(_keepalive_loop())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal _keepalive_task
        if _keepalive_task:
            _keepalive_task.cancel()
            _keepalive_task = None
        global _ttyd_proc
        logger.info("Client disconnected")
        if _ttyd_proc is not None:
            _ttyd_proc.terminate()
            _ttyd_proc = None
            logger.info("ttyd stopped")
        router.cleanup()
        # Clean up any ephemeral image temp files for this session
        if _image_tmp_root.exists():
            shutil.rmtree(_image_tmp_root, ignore_errors=True)
            logger.info(f"[IMAGE] Cleaned up temp dir {_image_tmp_root}")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)

    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport)


if __name__ == "__main__":
    from pathlib import Path

    import aiohttp
    from fastapi import Request, WebSocket
    from fastapi.responses import HTMLResponse, RedirectResponse, Response
    from pipecat.runner.run import app, main

    cockpit_html = (Path(__file__).parent / "cockpit.html").read_text()
    editor_html = (Path(__file__).parent / "editor.html").read_text()

    _poc_html: dict[str, str] = {}
    for _poc_file in (Path(__file__).parent / "poc").glob("*.html"):
        _poc_html[_poc_file.stem] = _poc_file.read_text()

    # PoC 5: in-memory autosave store (session_id → state dict)
    # In production this will be per-connection and persisted to disk.
    _autosave_state: dict = {}

    # PoC 1: serve editor.html
    @app.get("/editor", response_class=HTMLResponse, include_in_schema=False)
    async def editor():
        return editor_html

    # PoC 1 / 3 / 5: serve PoC test pages
    @app.get("/poc/{name}", response_class=HTMLResponse, include_in_schema=False)
    async def poc_page(name: str):
        html = _poc_html.get(name)
        if html is None:
            return Response(
                f"PoC page '{name}' not found. Available: {list(_poc_html)}", status_code=404
            )
        return html

    # PoC 5: autosave state endpoints
    @app.post("/api/autosave", include_in_schema=False)
    async def autosave_post(request: Request):
        body = await request.json()
        _autosave_state.clear()
        _autosave_state.update(body)
        return {"ok": True}

    @app.get("/api/autosave", include_in_schema=False)
    async def autosave_get():
        return _autosave_state if _autosave_state else {}

    @app.delete("/api/autosave", include_in_schema=False)
    async def autosave_delete():
        _autosave_state.clear()
        return {"ok": True}

    @app.get("/api/images/{session_id}/{filename}", include_in_schema=False)
    async def serve_temp_image(session_id: str, filename: str):
        from fastapi.responses import FileResponse
        p = Path("/tmp/cockpit-images") / session_id / filename
        if not p.exists() or not p.is_file():
            return Response("Not found", status_code=404)
        return FileResponse(p)

    @app.get(
        "/api/docs/{project_slug}/version/{version}/images/{filename}",
        include_in_schema=False,
    )
    async def serve_version_image(project_slug: str, version: int, filename: str):
        from fastapi.responses import FileResponse

        from doc_storage import docs_root
        p = docs_root() / project_slug / f"version_{version}" / "images" / filename
        if not p.exists() or not p.is_file():
            return Response("Not found", status_code=404)
        return FileResponse(p)

    @app.post("/api/reset-terminal", include_in_schema=False)
    async def reset_terminal():
        get_router().reset_session()
        ensure_terminal_running()
        return {"ok": True}

    @app.get("/cockpit", response_class=HTMLResponse, include_in_schema=False)
    async def cockpit():
        return cockpit_html

    @app.get("/", include_in_schema=False)
    @app.get("/client", include_in_schema=False)
    async def redirect_to_cockpit():
        return RedirectResponse(url="/cockpit")

    @app.api_route("/terminal/{path:path}", methods=["GET", "POST"], include_in_schema=False)
    @app.api_route("/terminal", methods=["GET", "POST"], include_in_schema=False)
    async def proxy_terminal(request: Request, path: str = ""):
        ensure_terminal_running()
        url = f"{TTYD_BASE}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection")
        }
        # ttyd may not be ready yet — retry for up to 3 seconds
        for attempt in range(6):
            try:
                async with aiohttp.ClientSession(auto_decompress=False) as session:
                    async with session.request(
                        method=request.method,
                        url=url,
                        headers=headers,
                        data=body,
                        allow_redirects=False,
                    ) as resp:
                        content = await resp.read()
                        skip = {"transfer-encoding", "connection"}
                        return Response(
                            content=content,
                            status_code=resp.status,
                            headers={
                                k: v for k, v in resp.headers.items() if k.lower() not in skip
                            },
                        )
            except aiohttp.ClientConnectorError:
                if attempt == 5:
                    return Response("Terminal is starting", status_code=503)
                await asyncio.sleep(0.5)

    @app.websocket("/terminal/ws")
    async def proxy_terminal_ws(ws: WebSocket):
        await ws.accept(subprotocol="tty")
        ensure_terminal_running()
        async with aiohttp.ClientSession() as session:
            try:
                ttyd_ws = await session.ws_connect(TTYD_WS_URL, protocols=["tty"])
            except aiohttp.ClientConnectorError:
                await ws.close(code=1013, reason="Terminal is starting")
                return
            async with ttyd_ws:

                async def client_to_ttyd():
                    try:
                        async for msg in ws.iter_bytes():
                            await ttyd_ws.send_bytes(msg)
                    except Exception:
                        pass

                async def ttyd_to_client():
                    try:
                        async for msg in ttyd_ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                await ws.send_bytes(msg.data)
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                await ws.send_text(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    except Exception:
                        pass

                done, pending = await asyncio.wait(
                    [
                        asyncio.ensure_future(client_to_ttyd()),
                        asyncio.ensure_future(ttyd_to_client()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

    main()
