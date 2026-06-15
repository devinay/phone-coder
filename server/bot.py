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
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMRunFrame,
    ManuallySwitchServiceFrame,
    OutputTransportMessageUrgentFrame,
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
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.processors.frameworks.rtvi.models import ServerMessage

from agent_router import AgentRouter
from diagram_focus import DiagramFocusStateMachine
from doc_state import DocStateMachine, StateMachineError
from git_storage import (
    atomic_write,
    create_project,
    docs_root,
    list_projects,
    load_project,
    load_document,
    save_document,
)
from doc_writer import AttributedUtterance
from helpers import (
    MERMAID_SUPPORTED_TYPES,
    MERMAID_UNSUPPORTED_TYPES,
    _strip_generated_sections,
    _replace_section,
    _validate_mermaid_source,
    _build_diagram_block,
    _insert_diagram_in_doc,
    _update_diagram_in_doc,
    _extract_diagram_source,
    _move_diagram_in_doc,
)
from processors import (
    SafeKokoroTTSService,
    CockpitPrinter,
    TTSGate,
    TTSState,
    ModelState,
    LLMCallInspector,
    InterceptHandler,
)
from prompt import build_system_prompt
from tools import (
    create_shell_tools,
    create_doc_tools,
    create_diagram_tools,
    create_image_tools,
    create_web_tools,
)

load_dotenv(override=True)

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

logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

for noisy in ("aiortc", "aioice", "aiohttp.client_ws", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


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

    # Speech-to-Text service. Diarization is intentionally disabled for now:
    # doc mode assumes a single user and records controller turns separately.
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(diarize=False, punctuate=True, smart_format=True),
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
        settings=SafeKokoroTTSService.Settings(
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

    # Get system prompt from the modular prompt module
    system_prompt = build_system_prompt()

    # Create context without tools initially (we'll pass tools to LLM services)
    context = LLMContext(messages=[{"role": "system", "content": system_prompt}])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    printer = CockpitPrinter()
    tts_gate = TTSGate(tts_state)

    # Pipeline (gate removed - was breaking LLMUserAggregator frame flow)
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
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

    # Now create all tools using the factories (after task is created)
    shell_tools = create_shell_tools(router)
    doc_tools = create_doc_tools(_doc_sm, _diagram_focus_sm, task, router)
    diagram_tools = create_diagram_tools(_doc_sm, _diagram_focus_sm, task, context)
    image_tools = create_image_tools(
        _diagram_focus_sm, _doc_sm, task, _session_id, _image_tmp_root
    )
    web_tools = create_web_tools()

    # Combine all tools into one dict
    all_tools = {
        **shell_tools,
        **doc_tools,
        **diagram_tools,
        **image_tools,
        **web_tools,
    }

    # Register tools with the LLM context so the model knows their schemas
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    tools = ToolsSchema(list(all_tools.values()))
    context.set_tools(tools)

    for _svc in (llm_openai, llm_anthropic, llm_ollama):
        for tool_name, tool_func in all_tools.items():
            _svc.register_direct_function(tool_func)

    # Set task context for processors
    printer.set_task_context(task, context, _doc_sm)

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
        "/api/docs/{project_slug}/images/{filename}",
        include_in_schema=False,
    )
    async def serve_doc_image(project_slug: str, filename: str):
        from fastapi.responses import FileResponse

        from git_storage import docs_root
        p = docs_root() / project_slug / "images" / filename
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
