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

from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    ErrorFrame,
    LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMRunFrame,
    LLMTextFrame, OutputTransportMessageUrgentFrame, TextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair, LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import processor as RTVI
from pipecat.processors.frameworks.rtvi.models import (
    BotTranscriptionMessage,
    ServerMessage,
    TextMessageData,
)
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.frames.frames import ManuallySwitchServiceFrame
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from agent_router import AgentRouter
from doc_state import DocStateMachine, StateMachineError, ALREADY_ACTIVE, INVALID_STATE
from doc_storage import create_project, load_project, load_version, atomic_write, docs_root
from doc_writer import AttributedUtterance

load_dotenv(override=True)

TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() == "true"
TTYD_PORT = int(os.getenv("TTYD_PORT", "7681"))
TTYD_BASE = f"http://127.0.0.1:{TTYD_PORT}"
TTYD_WS_URL = f"ws://127.0.0.1:{TTYD_PORT}/ws"

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logger.remove()  # remove default stdout sink
logger.add(sys.stdout, level="DEBUG", colorize=True)
logger.add(os.path.join(_LOG_DIR, "bot.log"), rotation="10 MB", retention="5 days", level="DEBUG", enqueue=True)


logger.info(f"Log file: {os.path.join(_LOG_DIR, 'bot.log')}")

class InterceptHandler(logging.Handler):
    def emit(self, record):
        logger.opt(depth=6, exception=record.exc_info).log(
            record.levelname, record.getMessage()
        )

logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

for noisy in ("aiortc", "aioice", "aiohttp.client_ws", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)



class CockpitPrinter(FrameProcessor):
    """Assembles LLM token stream, logs responses, and sends bot-transcription to UI."""

    def __init__(self):
        super().__init__()
        self._buffer: list[str] = []
        self._rtvi: RTVI.RTVIProcessor | None = None

    def set_rtvi(self, rtvi: RTVI.RTVIProcessor):
        self._rtvi = rtvi

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            # PoC 2: log raw result to inspect Deepgram diarization speaker field
            speaker = getattr(frame, "speaker", None)
            if speaker is None and frame.result is not None:
                try:
                    words = (frame.result or {}).get("words") or []
                    speakers = {w.get("speaker") for w in words if w.get("speaker") is not None}
                    speaker = ",".join(str(s) for s in sorted(speakers)) if speakers else None
                except Exception:
                    pass
            logger.info(f"[USER speaker={speaker}]: {frame.text}")

            # Feed DocWriter when in doc_mode
            session = _doc_sm.session
            if session.state.value == "doc_mode" and session.doc_writer:
                import time as _time
                utterance = AttributedUtterance(
                    text=frame.text,
                    timestamp=_time.time(),
                    speaker_id=str(speaker) if speaker is not None else None,
                    confidence=None,
                )
                session.doc_writer.add_utterance(utterance)
                session.doc_writer.set_speaker_map(session.speaker_map)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._buffer = []
        elif isinstance(frame, LLMTextFrame):
            self._buffer.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._buffer:
                text = "".join(self._buffer)
                logger.info(f"[CONTROLLER]: {text}")
                self._buffer = []
                if self._rtvi:
                    from pipecat.frames.frames import OutputTransportMessageUrgentFrame
                    msg = BotTranscriptionMessage(data=TextMessageData(text=text))
                    await self.push_frame(
                        OutputTransportMessageUrgentFrame(message=msg.model_dump()),
                        direction
                    )
            # Push LLMFullResponseEndFrame after bot-transcription so bot-llm-stopped
            # fires in the browser only after the text is already in the bubble.
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class TTSState:
    def __init__(self):
        self.enabled = TTS_ENABLED
        self.provider = os.getenv("TTS_PROVIDER", "cartesia")  # "cartesia" | "openai"
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


_router: AgentRouter | None = None
_ttyd_proc: subprocess.Popen | None = None
_doc_sm: DocStateMachine = DocStateMachine()


def get_router() -> AgentRouter:
    global _router
    if _router is None:
        _router = AgentRouter()
    return _router


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

    router = get_router()

    # Speech-to-Text service (PoC 2: diarize enabled — logs speaker field for validation)
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(diarize=True, punctuate=True, smart_format=True),
    )


    # Text-to-Speech services — switchable at runtime via UI
    tts_state = TTSState()
    tts_cartesia = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
        ),
    )
    tts_openai = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAITTSService.Settings(
            voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
        ),
    )
    tts_kokoro = KokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=os.getenv("KOKORO_VOICE", "af_heart"),
        ),
    )
    _tts_services = {"cartesia": tts_cartesia, "openai": tts_openai, "kokoro": tts_kokoro}
    tts = ServiceSwitcher(
        services=[tts_cartesia, tts_openai, tts_kokoro],
        strategy_type=ServiceSwitcherStrategyManual,
    )

    # LLM service
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
    )

    async def run_command(params: FunctionCallParams, command: str, directory_path: str):
        """Run a shell command in the terminal. Use this for everything: starting assistants
        (e.g. 'claude', 'codex'), running build tools, git commands, etc.

        Args:
            command: The verbatim shell command to run (e.g. 'claude', 'ls -la', 'git status')
            directory_path: Absolute path to the directory to run the command in
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
                    await params.result_callback("ERROR: topic_name is required when action='create'.")
                    return
                project_info, version_info = create_project(topic_name)
                logger.info(f"[DOC] Created project '{project_info.slug}' at {project_info.project_dir}")
            elif action == "open":
                slug = project_slug or topic_name
                if not slug:
                    await params.result_callback("ERROR: project_slug is required when action='open'.")
                    return
                project_info = load_project(slug)
                if project_info is None:
                    await params.result_callback(f"PROJECT_NOT_FOUND: No project with slug '{slug}'.")
                    return
                version_info = load_version(project_info.project_dir, project_info.current_version)
                logger.info(f"[DOC] Opened project '{project_info.slug}' version {project_info.current_version}")
            else:
                await params.result_callback(f"ERROR: Unknown action '{action}'. Use 'create' or 'open'.")
                return

            _doc_sm.enter_doc_mode(
                project_slug=project_info.slug,
                version_info=version_info,
                project_dir=project_info.project_dir,
            )

            # Push browser event
            from pipecat.processors.frameworks.rtvi.models import ServerMessage
            msg = ServerMessage(data={
                "type": "doc-mode-entered",
                "project_slug": project_info.slug,
                "version": version_info.version if version_info else 0,
            })
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
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

        try:
            _doc_sm.exit_doc_mode()  # raises if not in doc_mode
        except StateMachineError as e:
            logger.error(f"[DOC] State machine error in exit_doc_mode: {e}")
            await params.result_callback(f"{e.code}: {e.message}")
            return

        if not discard and session.version_info and session.doc_writer:
            try:
                from doc_storage import atomic_write
                vi = session.version_info
                doc_content = session.doc_writer.render_document_md()
                transcript_content = session.doc_writer.render_transcript_md()
                atomic_write(vi.document_md, doc_content)
                atomic_write(vi.transcript_md, transcript_content)

                import json
                atomic_write(vi.speakers_json, json.dumps(session.speaker_map, indent=2))

                logger.info(
                    f"[DOC] Saved {session.doc_writer.utterance_count()} utterances "
                    f"to {vi.document_md}"
                )
            except Exception as e:
                logger.error(f"[DOC] Save failed: {e}")
                _doc_sm.enter_error_recovery()
                await params.result_callback(f"SAVE_FAILED: {e}")
                return

        _doc_sm.complete_save()

        # Push browser event
        from pipecat.processors.frameworks.rtvi.models import ServerMessage
        msg = ServerMessage(data={"type": "doc-mode-exited"})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info("[DOC STATE] doc_mode → shell")

        # Verify terminal is still responsive
        router = get_router()
        router.capture_output(5)

        action_taken = "discarded" if discard else "saved"
        await params.result_callback(
            f"Documentation mode closed. Session {action_taken}. Terminal is restored."
        )

    llm.register_direct_function(run_command)
    llm.register_direct_function(send_input)
    llm.register_direct_function(capture_output)
    llm.register_direct_function(find_directory)
    llm.register_direct_function(enter_doc_mode)
    llm.register_direct_function(exit_doc_mode)

    tools = ToolsSchema([run_command, send_input, capture_output, find_directory,
                         enter_doc_mode, exit_doc_mode])

    system_prompt = (
        "You are the controller for a local voice coding cockpit. "
        "The terminal on the right is a single fish shell running inside a tmux session.\n\n"
        "TOOLS:\n"
        "- run_command(command, directory_path): run a shell command in the terminal\n"
        "- send_input(text): send text to whatever is currently running in the terminal\n"
        "- capture_output(lines): read recent output from the terminal\n"
        "- find_directory(name): find a directory by partial name\n"
        "- enter_doc_mode(action, topic_name, project_slug): enter Documentation Mode\n"
        "  - action='create' + topic_name: start a new documentation project\n"
        "  - action='open' + project_slug: open an existing project\n"
        "- exit_doc_mode(discard): save and close Documentation Mode\n\n"
        "WORKFLOW:\n"
        "1. When the user names a directory, use find_directory first to confirm the full path.\n"
        "2. Confirm with the user before proceeding if the match isn't exact.\n"
        "3. After running a command, capture_output and summarize what happened.\n"
        "4. When the user wants to document something, call enter_doc_mode. "
        "   When they are done, call exit_doc_mode.\n\n"
        "SAFETY:\n"
        "1. Never run destructive commands (rm -rf, git reset --hard, etc.) without explicit confirmation.\n"
        "2. Do not auto-commit unless asked.\n"
        "3. Be concise and direct in your replies."
    )

    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt}],
        tools=tools
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),    ),
    )

    printer = CockpitPrinter()
    tts_gate = TTSGate(tts_state)

    # Pipeline
    pipeline = Pipeline([
        transport.input(),

        stt,

        user_aggregator,

        llm,

        printer,

        tts_gate,

        tts,

        transport.output(),

        assistant_aggregator,
    ])


    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    printer.set_rtvi(task.rtvi)

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

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await send_tts_status("Text-only mode is active.")
        # Kick off the conversation
        context.add_message({"role": "user", "content": "Please introduce yourself as the Voice Coding Cockpit controller."})
        await task.queue_frames([LLMRunFrame()])

    @task.rtvi.event_handler("on_client_message")
    async def on_client_message(rtvi, message):
        data = message.data or {}
        if message.type == "tts-toggle":
            enabled = bool(data.get("enabled"))
            tts_state.set_enabled(enabled)
            logger.info(f"TTS {'enabled' if enabled else 'disabled'} by browser toggle")
            await send_tts_status("Voice enabled." if enabled else "Text-only mode is active.")
        elif message.type == "tts-provider":
            provider = data.get("provider", "cartesia")
            if provider not in _tts_services:
                return
            tts_state.provider = provider
            service = _tts_services[provider]
            await task.queue_frames([ManuallySwitchServiceFrame(service=service)])
            logger.info(f"TTS provider switched to {provider}")
            await send_tts_status(f"Switched to {provider.capitalize()} TTS.")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — starting tmux + ttyd")
        ensure_terminal_running(ttyd_port)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        global _ttyd_proc
        logger.info("Client disconnected")
        if _ttyd_proc is not None:
            _ttyd_proc.terminate()
            _ttyd_proc = None
            logger.info("ttyd stopped")
        router.cleanup()
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
    from fastapi import Request, WebSocket
    from fastapi.responses import HTMLResponse, RedirectResponse, Response
    from pipecat.runner.run import app, main
    import aiohttp

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
            return Response(f"PoC page '{name}' not found. Available: {list(_poc_html)}", status_code=404)
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
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection")}
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
                            headers={k: v for k, v in resp.headers.items() if k.lower() not in skip},
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
                    async for msg in ws.iter_bytes():
                        await ttyd_ws.send_bytes(msg)

                async def ttyd_to_client():
                    async for msg in ttyd_ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                done, pending = await asyncio.wait(
                    [asyncio.ensure_future(client_to_ttyd()), asyncio.ensure_future(ttyd_to_client())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

    main()
