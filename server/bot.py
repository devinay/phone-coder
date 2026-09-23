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
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    LLMRunFrame,
    LLMUpdateSettingsFrame,
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
from pipecat.processors.frameworks.rtvi.models import ServerMessage
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from agent_router import AgentRouter
from agents import AgentRuntime, AgentTurnResetter, build_default_registry
from catalog import ModelInfo, build_catalog, grouped, price_of
from diagram_focus import DiagramFocusStateMachine
from doc_state import DocStateMachine
from git_storage import (
    atomic_write,
)
from helpers import (
    _update_diagram_in_doc,
)
from memory import append_summary, render_prompt_suffix, summarize_session
from processors import (
    CockpitPrinter,
    InterceptHandler,
    LLMCallInspector,
    ModelState,
    SafeKokoroTTSService,
    TerminalStatusInjector,
    TranscriptNormaliser,
    TTSGate,
    TTSState,
)
from terminal_vocab import STT_KEYTERMS
from tools import (
    create_diagram_tools,
    create_doc_tools,
    create_image_tools,
    create_shell_tools,
    create_web_tools,
)

load_dotenv(override=True)

# Speech-to-text engine. Only Deepgram is wired up; the setting is kept so an
# alternative can be slotted in without touching call sites.
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").lower()
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe")

TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() == "true"
# Speech rate applied to every TTS provider that supports one.
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.25"))
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

for noisy in (
    "aiortc",
    "aioice",
    "aiohttp.client_ws",
    "websockets",
    # the HTTP stack traces every connect/TLS/read at DEBUG
    "httpx",
    "httpcore",
):
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

# Where .env lives, read at import so the value is available before run_bot.
ENV_PATH = Path(__file__).parent / ".env"

# Fetched from the providers themselves, so the picker tracks what the account
# can actually reach instead of a list that goes stale on every release. Two
# best-effort HTTP calls; an unreachable provider simply contributes nothing.
# Ollama is added by hand because a local runtime has no catalogue endpoint.
# Whatever is configured must survive the shortlist: an id outside the catalogue
# is rejected, so hiding the running model would break it rather than tidy it.
_CONFIGURED_MODELS = {os.getenv("LLM_MODEL", "")} | {
    os.getenv(f"AGENT_MODEL_{a.upper()}", "")
    for a in ("controller", "shell", "doc", "diagram", "image", "web")
}
CATALOG = build_catalog(
    anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
    openai_key=os.getenv("OPENAI_API_KEY", ""),
    # price_of() gives the local model a real zero; without it the picker
    # says "price unknown" for something that is genuinely free.
    extra=[ModelInfo(id=m, provider="ollama", price=price_of(m)) for m in _OLLAMA_MODELS],
    # The full fetched list is a scroll rather than a choice; MODEL_SHORTLIST=off
    # restores it.
    shortlist=os.getenv("MODEL_SHORTLIST", "on").lower() != "off",
    in_use={m for m in _CONFIGURED_MODELS if m},
)
# Falls back to the static list when neither provider answered, so the cockpit
# is never left with an empty dropdown.
if not CATALOG:
    CATALOG = {m: ModelInfo(id=m, provider="openai") for m in _MODEL_PRICING}
    logger.warning("[CATALOG] no provider reachable; falling back to the built-in list")
MODEL_PROVIDERS = {i: info.provider for i, info in CATALOG.items()}

DEFAULT_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")


def agent_model(agent_id: str, fallback: str) -> str:
    """Per-agent model override, e.g. AGENT_MODEL_SHELL=gpt-4.1.

    Lets one agent run on a stronger or cheaper model than the rest without a
    code change; unset agents share the default.
    """
    return os.getenv(f"AGENT_MODEL_{agent_id.upper()}", fallback)


def _log_controller_models() -> None:
    """Announce, at startup, which models the controller can actually reach.

    Also reports which provider keys are present, since a missing key shows up
    otherwise only as a failure on the first turn.
    """
    logger.info(
        f"[CONTROLLER] config file: {ENV_PATH} "
        f"({'found' if ENV_PATH.exists() else 'NOT FOUND — using shell environment only'})"
    )
    for provider, ids in grouped(CATALOG).items():
        logger.info(f"[CONTROLLER] {provider:10s} ({len(ids):2d}): {', '.join(ids)}")
    for name, var in (("OpenAI", "OPENAI_API_KEY"), ("Anthropic", "ANTHROPIC_API_KEY")):
        # Names only — whether a key is present is useful at startup, the value
        # never is.
        logger.info(f"[CONTROLLER] {name}: {'key set' if os.getenv(var) else 'NO KEY'}")
    logger.info(f"[CONTROLLER] default model: {DEFAULT_MODEL}")


_log_controller_models()


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


async def run_bot(transport: BaseTransport, ttyd_port: int = TTYD_PORT):
    """Main bot logic."""
    logger.info("Starting bot")

    # Unique ID for this bot session — used to namespace ephemeral image temp dirs
    _session_id = uuid.uuid4().hex[:12]
    _image_tmp_root = Path("/tmp/cockpit-images") / _session_id

    router = get_router()
    # Reset rather than reuse. A clean disconnect already tears the session
    # down, but a crash or a Ctrl-C leaves it standing with whatever was running
    # still in it — and the controller is told at startup that an empty terminal
    # is attached. Resetting here is what makes that true rather than usual.
    _killed_at_startup = router.ensure_session(reset=True)
    # Start recording the pane now, so history exists before the first command.
    # Needs a running loop, which is why it happens here rather than in
    # ensure_terminal_running.
    router.start_history()

    # Speech-to-Text service. Diarization is intentionally disabled for now:
    # doc mode assumes a single user and records controller turns separately.
    if True:
        # keyterms bias the recogniser toward proper nouns and unix tools that
        # otherwise lose to commoner English words — "claude" to "cloud" above
        # all. TranscriptNormaliser repairs what still slips through.
        # keyterm needs nova-3, which is pipecat's default model here. Settings
        # replaces the deprecated live_options and is the only path that carries
        # keyterm through.
        stt = DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            settings=DeepgramSTTService.Settings(
                diarize=False,
                punctuate=True,
                smart_format=True,
                keyterm=STT_KEYTERMS,
            ),
        )
        logger.info(
            f"[STT] engine=deepgram model=nova-3 (streaming, interim results) "
            f"keyterms={len(STT_KEYTERMS)}"
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
    # One speech rate across every provider that can honour it. Each vendor
    # expresses it differently, and Deepgram has no speed control at all, so it
    # simply speaks at its own pace.
    if not 0.6 <= TTS_SPEED <= 1.5:
        logger.warning(
            f"TTS_SPEED={TTS_SPEED} is outside the range every provider accepts "
            "(0.6–1.5); Cartesia will reject values beyond its own limits"
        )
    logger.info(f"[TTS] speech rate {TTS_SPEED}x (Deepgram does not support rate control)")

    tts_cartesia = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
            # Sonic-3 treats this as guidance rather than a strict multiplier.
            generation_config=GenerationConfig(speed=TTS_SPEED),
        ),
        text_filters=[_md_filter()],
    )
    tts_openai = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAITTSService.Settings(
            voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
            speed=TTS_SPEED,
        ),
        text_filters=[_md_filter()],
    )
    # fp16 halves the model size and is ~18% faster to first audio than fp32
    # with no audible quality difference (int8 is 4x slower on Apple silicon —
    # dequantisation overhead — so it is deliberately not used). Falls back to
    # whatever pipecat auto-downloads if the fp16 file is absent.
    _kokoro_fp16 = Path.home() / ".cache" / "kokoro-onnx" / "kokoro-v1.0.fp16.onnx"
    tts_kokoro = SafeKokoroTTSService(
        model_path=str(_kokoro_fp16) if _kokoro_fp16.exists() else None,
        speed=float(os.getenv("KOKORO_SPEED", str(TTS_SPEED))),
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
    # Pass the model explicitly: processors' own default is read at import,
    # before load_dotenv(), so it would miss LLM_MODEL from .env.
    model_state = ModelState(
        model=DEFAULT_MODEL,
        providers=MODEL_PROVIDERS,
        prices={i: info.price for i, info in CATALOG.items() if info.price},
    )
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
    _llm_services = [llm_openai, llm_anthropic, llm_ollama]

    llm = ServiceSwitcher(
        services=_llm_services,
        strategy_type=ServiceSwitcherStrategyManual,
    )
    inspector = LLMCallInspector(model_state)

    # Per-agent models, e.g. AGENT_MODEL_SHELL=gpt-4.1. Unset agents share
    # DEFAULT_MODEL.
    agent_models = {
        spec_id: agent_model(spec_id, DEFAULT_MODEL)
        for spec_id in ("controller", "shell", "doc", "diagram", "image", "web")
    }
    agent_models = {k: v for k, v in agent_models.items() if v != DEFAULT_MODEL}
    if agent_models:
        logger.info(f"[AGENT MODEL] per-agent overrides: {agent_models}")

    agent_registry = build_default_registry(
        DEFAULT_MODEL,
        extra_models=sorted(CATALOG),
        prompt_suffix=render_prompt_suffix(),
        agent_models=agent_models,
    )
    agent_runtime = AgentRuntime(agent_registry)
    system_prompt = agent_registry.get("controller").prompt_text

    # The terminal block is normally refreshed by TerminalStatusInjector, but it
    # only fires on a turn — so before the user's first word the controller had
    # no idea a terminal existed, and would say so if asked. Seed it here, under
    # the same marker the injector rewrites in place, so turn one already knows
    # what is attached and where it is.
    #
    # The working directory comes from tmux's pane_current_path rather than from
    # running `pwd`: it is the same answer, it does not type into the user's
    # pane, and it still works when a full-screen program owns it.
    try:
        opening_state = router.terminal_context_block()
        if _killed_at_startup:
            opening_state += (
                "\n[NOTE] A previous session was still running "
                f"{', '.join(_killed_at_startup)} and was reset, so the terminal "
                "is empty now. Mention this if the user seems to expect it."
            )
        system_prompt += TerminalStatusInjector.MARKER + opening_state
        logger.info(f"[TERMINAL STATUS] seeded at startup: {opening_state}")
    except Exception as e:
        # A probe failure must not stop the bot coming up.
        logger.warning(f"[TERMINAL STATUS] could not seed the opening prompt: {e}")

    # Create context without tools initially (we'll pass tools to LLM services)
    context = LLMContext(messages=[{"role": "system", "content": system_prompt}])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    printer = CockpitPrinter(runtime=agent_runtime)
    tts_gate = TTSGate(tts_state)

    # Pipeline (gate removed - was breaking LLMUserAggregator frame flow)
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            TranscriptNormaliser(),
            user_aggregator,
            TerminalStatusInjector(router, context),
            inspector,
            llm,
            printer,
            tts_gate,
            tts,
            transport.output(),
            assistant_aggregator,
            AgentTurnResetter(agent_runtime),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # Going quiet is not a reason to tear down the session — the terminal,
        # tmux state and open documents all outlive a silent stretch. Idle
        # detection stays on, but it mutes the mic instead of cancelling.
        cancel_on_idle_timeout=False,
    )

    async def announce_terminal(reason: str, text: str) -> None:
        """Surface a background terminal wake on its own channel.

        A monitor report is the one message the user did not ask for and is not
        waiting on, which makes it the one that must not arrive silently — and
        it could: text-only mode marks replies skip_tts, and a quiet spell
        idle-mutes the mic. This goes out as an urgent server message, so the
        cockpit shows it whatever the voice settings are.
        """
        msg = ServerMessage(data={"type": "terminal-alert", "reason": reason, "text": text})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[ALERT] terminal-alert reason={reason} text={text!r}")

    # Now create all tools using the factories (after task is created)
    shell_tools = create_shell_tools(
        router,
        task=task,
        context=context,
        # Lets the monitor wake as an agent that can press a key, rather than
        # waking as the controller and having to describe the keypress.
        runtime=agent_runtime,
        announce=announce_terminal,
    )
    doc_tools = create_doc_tools(_doc_sm, _diagram_focus_sm, task, router)
    diagram_tools = create_diagram_tools(_doc_sm, _diagram_focus_sm, task, context)
    image_tools = create_image_tools(
        _diagram_focus_sm, _doc_sm, task, _session_id, _image_tmp_root
    )
    web_tools = create_web_tools()

    async def switch_llm_model(new_model: str, preserve_context: bool = True) -> str:
        logger.info(
            f"[AGENT MODEL] switch_requested model={new_model} "
            f"active_agent={agent_runtime.active_agent_id} preserve_context={preserve_context}"
        )
        if new_model not in CATALOG and new_model not in _MODEL_PRICING:
            logger.warning(f"Unknown model requested: {new_model}")
            return f"MODEL_UNKNOWN: {new_model}"

        old_provider = model_state.provider
        old_family = model_state.vendor_family
        model_state.model = new_model
        new_provider = model_state.provider
        new_family = model_state.vendor_family
        target_svc = _llm_by_provider[new_provider]
        settings_frame = None

        if hasattr(target_svc, "set_full_model_name"):
            target_svc.set_full_model_name(new_model)
        else:
            settings_frame = LLMUpdateSettingsFrame(
                service=target_svc,
                delta=target_svc.Settings(model=new_model),
            )

        # Two independent questions. The pipeline service only changes when the
        # provider changes, and the context must be reset whenever the upstream
        # vendor changes, because vendors disagree on tool-call and message
        # encoding. With every model reaching its vendor directly these now move
        # together, but they are still distinct questions.
        needs_service_switch = old_provider != new_provider
        needs_reset = old_family != new_family

        if needs_reset:
            agent_runtime.apply_agent(
                agent_runtime.active_agent_id,
                user_request="The user switched AI models mid-conversation. Continue naturally.",
                preserve_context=False,
            )
        elif not preserve_context:
            agent_runtime.apply_agent(agent_runtime.active_agent_id, preserve_context=False)

        frames = []
        if needs_service_switch:
            frames.append(ManuallySwitchServiceFrame(service=target_svc))
        if settings_frame is not None:
            frames.append(settings_frame)
        if frames:
            await task.queue_frames(frames)

        detail = f"provider {old_provider}→{new_provider}" if needs_service_switch else new_provider
        detail += f", vendor {old_family}→{new_family}, context reset" if needs_reset else ""
        msg = f"LLM switched to {new_model} ({detail})"
        logger.info(msg)
        return msg

    admin_tools = agent_runtime.create_admin_tools()

    # Combine all tools into one dict
    all_tools = {
        **admin_tools,
        **shell_tools,
        **doc_tools,
        **diagram_tools,
        **image_tools,
        **web_tools,
    }

    guarded_tools = agent_runtime.bind(
        context=context,
        task=task,
        tools=all_tools,
        model_switcher=switch_llm_model,
    )

    for _svc in _llm_services:
        for tool_name, tool_func in guarded_tools.items():
            _svc.register_direct_function(tool_func)
    logger.info(
        f"[AGENT] registered {len(guarded_tools)} guarded tools with "
        f"{len(_llm_services)} LLM services"
    )

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

    async def _point_switcher_at_default():
        """Select the service that actually serves the configured default model.

        ServiceSwitcher begins on the first service in its list; if the default
        model belongs to a different provider, the first request would go to the
        wrong one.
        """
        service = _llm_by_provider.get(model_state.provider)
        if service is not None and service is not _llm_services[0]:
            logger.info(f"[MODEL] starting on {model_state.provider} for {model_state.model}")
            await task.queue_frames([ManuallySwitchServiceFrame(service=service)])

    async def send_model_catalog():
        """Replace the cockpit's static dropdown with the fetched catalogue.

        Sent on connect, so the picker reflects what the configured keys can
        actually reach rather than a list compiled into the page.
        """
        groups = grouped(CATALOG)
        # Anything the pipeline cannot drive is still listed but marked, so the
        # picker is honest rather than quietly incomplete.
        unsupported = {
            i: "no tool support" for i, info in CATALOG.items() if not info.supports_tools
        }
        msg = ServerMessage(
            data={
                "type": "model-catalog",
                "groups": groups,
                "unsupported": unsupported,
                "prices": {i: list(info.price) for i, info in CATALOG.items() if info.price},
                "model": model_state.model,
            }
        )
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(
            f"[CATALOG] sent {len(CATALOG)} models in {len(groups)} groups to the cockpit "
            f"({len(unsupported)} marked unsupported)"
        )

    async def send_model_status():
        msg = ServerMessage(data={"type": "model-status", "model": model_state.model})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])

    @task.event_handler("on_idle_timeout")
    async def on_idle_timeout(task_):
        """Mute the microphone rather than dropping the session.

        Asks the cockpit to enter exactly the state the on-screen mute button
        produces, so unmuting works the same way afterwards.
        """
        logger.info("[IDLE] no activity — muting microphone (session kept alive)")
        msg = ServerMessage(
            data={"type": "idle-mute", "reason": "Muted after a quiet spell — click to resume."}
        )
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await send_tts_status("Text-only mode is active.")
        # ServiceSwitcher starts on the first service, which is OpenAI. Point it
        # at the right provider before the opening turn runs, so a default model
        # from another vendor is not sent to OpenAI.
        await _point_switcher_at_default()
        await send_model_status()
        await send_model_catalog()
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
            await switch_llm_model(new_model, preserve_context=False)
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

        # The cockpit does not complete the RTVI handshake, so on_client_ready
        # never fires; the initial state the UI needs is pushed from the
        # transport-level connect instead.
        await send_model_status()
        await send_model_catalog()
        await _point_switcher_at_default()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal _keepalive_task
        if _keepalive_task:
            _keepalive_task.cancel()
            _keepalive_task = None
        global _ttyd_proc
        logger.info("Client disconnected")

        # Summarise before teardown, while the context is still intact. A memory
        # failure must never prevent the rest of the cleanup from running.
        try:
            summary = await summarize_session(context.messages or [])
            if summary:
                append_summary(summary)
        except Exception as e:
            logger.warning(f"[MEMORY] session summary skipped: {e}")

        if _ttyd_proc is not None:
            _ttyd_proc.terminate()
            _ttyd_proc = None
            logger.info("ttyd stopped")
        # Stop the background watches before the session goes. Their loops poll
        # tmux, so leaving them running past cleanup() meant an error logged
        # every tick against a pane that had just been killed.
        if getattr(router, "monitor", None) is not None:
            router.monitor.stop("session ending")
        router.watches.clear()
        # Detaches the pane tap and deletes the spool: terminal history must not
        # outlive the session that produced it.
        await router.stop_history()
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
        # The page carries all the client JS, so a cached copy silently runs
        # against a newer server. Cheap to re-send; never worth caching.
        return HTMLResponse(
            cockpit_html,
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )

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
