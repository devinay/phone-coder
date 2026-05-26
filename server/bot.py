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
import subprocess
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMRunFrame,
    LLMTextFrame, TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair, LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi import processor as RTVI
from pipecat.processors.frameworks.rtvi.models import BotTranscriptionMessage, TextMessageData
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from agent_router import AgentRouter
from dual_stt import LanguageGate, WhisperSideCar

load_dotenv(override=True)

TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower() == "true"

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
            logger.info(f"[USER]: {frame.text}")
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


async def run_bot(transport: BaseTransport, ttyd_port: int = 7681):
    """Main bot logic."""
    logger.info("Starting bot")

    router = AgentRouter()
    ttyd_proc = None

    # Dual-mode STT processors
    whisper_sidecar = WhisperSideCar(api_key=os.getenv("OPENAI_API_KEY"))
    language_gate = LanguageGate()

    # Speech-to-Text service
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))


    # Text-to-Speech service
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
        ),
    )

    # LLM service
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
    )

    async def open_pane(params: FunctionCallParams, pane_name: str):
        """Open a new terminal pane (tmux window) with the given name.
        Use this before starting a new AI assistant so each gets its own dedicated terminal.
        Example: open_pane('claude'), open_pane('codex'), open_pane('gemini').

        Args:
            pane_name: Short name for the pane (e.g. 'claude', 'codex', 'gemini')
        """
        target = router.open_pane(pane_name)
        result = f"Opened pane '{pane_name}' (target: {target}). Open panes: {', '.join(router.list_panes())}"
        logger.info(f"[TOOL] OPEN PANE: {pane_name} | {result}")
        await params.result_callback(result)

    async def list_panes(params: FunctionCallParams):
        """List all currently open terminal panes."""
        panes = router.list_panes()
        result = f"Open panes: {', '.join(panes) if panes else 'none'}"
        print(f"\n[TOOL] LIST PANES\n{result}\n")
        await params.result_callback(result)

    async def run_command(params: FunctionCallParams, command: str, directory_path: str, pane_name: str = "shell"):
        """Run a shell command in a terminal pane. Use this for everything: starting assistants
        (e.g. 'claude', 'codex', 'gemini'), running build tools, git commands, etc.

        Args:
            command: The verbatim shell command to run (e.g. 'claude', 'ls -la', 'git status')
            directory_path: Absolute path to the directory to run the command in
            pane_name: Which terminal pane to use (default: 'shell'). Use the pane name you opened with open_pane.
        """
        result = await router.run_command(command, directory_path, pane_name=pane_name)
        print(f"\n[TOOL] RUN COMMAND [{pane_name}]: {command}\n{result}\n")
        await params.result_callback(result)

    async def send_input(params: FunctionCallParams, text: str, pane_name: str = "shell"):
        """Send text input to whatever is currently running in a terminal pane.
        Use this to interact with an interactive program (e.g. sending a prompt to claude).

        Args:
            text: The text to send
            pane_name: Which terminal pane to send to (default: 'shell')
        """
        result = await router.send_input(text, pane_name=pane_name)
        print(f"\n[TOOL] SEND INPUT [{pane_name}]: {text}\n{result}\n")
        await params.result_callback(result)

    async def capture_output(params: FunctionCallParams, lines: int = 50, pane_name: str = "shell"):
        """Capture recent terminal output to check what happened.

        Args:
            lines: Number of lines to capture (default 50)
            pane_name: Which terminal pane to read from (default: 'shell')
        """
        result = router.capture_output(lines, pane_name=pane_name)
        print(f"\n[TOOL] CAPTURE OUTPUT [{pane_name}]\n{result}\n")
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

    async def set_language(params: FunctionCallParams, language: str):
        """Switch the speech recognition language.
        Call this when the user says they want to switch to a non-English language
        (e.g. 'I am switching to Kannada') or back to English ('switch back to English').
        Recognise the intent regardless of how it is phrased or which language it is said in.
        Supported values: 'english', 'kannada', 'hindi', 'tamil', 'telugu'.

        Args:
            language: Target language, lowercase (e.g. 'kannada', 'english')
        """
        whisper_sidecar.set_language(language)
        language_gate.set_language(language)
        result = f"Switched to {language} mode. {'Whisper is now active.' if language != 'english' else 'Deepgram is now active.'}"
        print(f"\n[TOOL] SET LANGUAGE: {language}\n")
        await params.result_callback(result)

    llm.register_direct_function(open_pane)
    llm.register_direct_function(list_panes)
    llm.register_direct_function(run_command)
    llm.register_direct_function(send_input)
    llm.register_direct_function(capture_output)
    llm.register_direct_function(find_directory)
    llm.register_direct_function(set_language)

    tools = ToolsSchema([open_pane, list_panes, run_command, send_input, capture_output, find_directory, set_language])

    system_prompt = (
        "You are the controller for a local voice coding cockpit. "
        "The terminal on the right is a tmux session with one or more windows (panes). "
        "Each pane is an independent fish shell — you can run different AI assistants side by side.\n\n"
        "TOOLS:\n"
        "- open_pane(name): create a new named terminal pane (e.g. 'claude', 'codex', 'gemini')\n"
        "- list_panes(): see all open panes\n"
        "- run_command(command, directory_path, pane_name): run a shell command in a specific pane\n"
        "- send_input(text, pane_name): send text to whatever is running in a pane\n"
        "- capture_output(lines, pane_name): read recent output from a pane\n"
        "- find_directory(name): find a directory by partial name\n\n"
        "MULTI-PANE WORKFLOW:\n"
        "1. To start a new assistant (Claude, Codex, Gemini, etc.), first call open_pane with its name.\n"
        "2. Then call run_command with that pane_name to launch the assistant in its own window.\n"
        "3. When sending prompts or reading output, always specify the correct pane_name.\n"
        "4. The user can switch between panes in the terminal with tmux shortcuts (Ctrl+b then number).\n\n"
        "GENERAL WORKFLOW:\n"
        "1. When the user names a directory, use find_directory first to confirm the full path.\n"
        "2. Confirm with the user before proceeding if the match isn't exact.\n"
        "3. After running a command, capture_output and summarize what happened.\n\n"
        "LANGUAGE SWITCHING:\n"
        "- Default mode is English (Deepgram STT).\n"
        "- When the user says 'I am switching to Kannada/Hindi/Tamil/Telugu' (or any equivalent phrasing), call set_language with the language name.\n"
        "- When the user says 'switch back to English' (in any language), call set_language('english').\n"
        "- After switching, all commands (run_command, send_input, etc.) work exactly the same.\n"
        "- Supported non-English languages: kannada, hindi, tamil, telugu.\n"
        "- When the user speaks in a non-English language, always include an English translation of their utterance at the start of your reply, tagged exactly like this: <Translation>their words in English</Translation>\n\n"
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

    # Pipeline
    pipeline = Pipeline([
        transport.input(),

        whisper_sidecar,   # taps audio for Whisper in non-English mode
        stt,               # Deepgram — always connected
        language_gate,     # gates Deepgram output based on active language

        user_aggregator,

        llm,

        printer,

        *([tts] if TTS_ENABLED else []),

        transport.output(),

        assistant_aggregator,
    ])


    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    printer.set_rtvi(task.rtvi)

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        # Kick off the conversation
        context.add_message({"role": "user", "content": "Please introduce yourself as the Voice Coding Cockpit controller."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal ttyd_proc
        logger.info("Client connected — starting tmux + ttyd")
        router.ensure_session()
        if ttyd_proc is None or ttyd_proc.returncode is not None:
            ttyd_proc = subprocess.Popen(
                ["ttyd", "--port", str(ttyd_port), "--writable",
                 "tmux", "attach-session", "-t", AgentRouter.SESSION],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info(f"ttyd started (pid {ttyd_proc.pid}) on port {ttyd_port}")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal ttyd_proc
        logger.info("Client disconnected")
        if ttyd_proc is not None:
            ttyd_proc.terminate()
            ttyd_proc = None
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

    TTYD_BASE = "http://localhost:7681"

    cockpit_html = (Path(__file__).parent / "cockpit.html").read_text()

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
                    raise
                await asyncio.sleep(0.5)

    @app.websocket("/terminal/ws")
    async def proxy_terminal_ws(ws: WebSocket):
        await ws.accept(subprotocol="tty")
        ttyd_ws_url = f"ws://localhost:7681/ws"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ttyd_ws_url, protocols=["tty"]) as ttyd_ws:
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
