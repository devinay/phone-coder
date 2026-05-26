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
import os
import subprocess

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
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from agent_router import AgentRouter

load_dotenv(override=True)



class CockpitPrinter(FrameProcessor):
    """Assembles LLM token stream and prints complete responses to the terminal."""

    def __init__(self):
        super().__init__()
        self._buffer: list[str] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            print(f"\n[USER]: {frame.text}", flush=True)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._buffer = []
        elif isinstance(frame, LLMTextFrame):
            self._buffer.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._buffer:
                print(f"\n[CONTROLLER]: {''.join(self._buffer)}\n", flush=True)
                self._buffer = []

        await self.push_frame(frame, direction)


async def run_bot(transport: BaseTransport, ttyd_port: int = 7681):
    """Main bot logic."""
    logger.info("Starting bot")

    router = AgentRouter()
    ttyd_proc = None

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
        print(f"\n[TOOL] OPEN PANE: {pane_name}\n{result}\n")
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

    llm.register_direct_function(open_pane)
    llm.register_direct_function(list_panes)
    llm.register_direct_function(run_command)
    llm.register_direct_function(send_input)
    llm.register_direct_function(capture_output)
    llm.register_direct_function(find_directory)

    tools = ToolsSchema([open_pane, list_panes, run_command, send_input, capture_output, find_directory])

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

    # Pipeline - assembled from reusable components
    pipeline = Pipeline([
        transport.input(),

        stt,

        user_aggregator,

        llm,

        printer,

        tts,

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
        async with aiohttp.ClientSession(auto_decompress=False) as session:
            async with session.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection")},
                data=await request.body(),
                allow_redirects=False,
            ) as resp:
                content = await resp.read()
                skip = {"transfer-encoding", "connection"}
                return Response(
                    content=content,
                    status_code=resp.status,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in skip},
                )

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
