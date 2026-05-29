# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

A **Voice Coding Cockpit** — a browser-based UI where the user speaks commands and an AI voice agent (GPT-4o) executes them in a local terminal. The agent controls a tmux session via tool calls and speaks responses back via TTS.

## Commands

All commands run from `server/`:

```bash
# Install dependencies
uv sync

# Run the bot (opens at http://localhost:7860/cockpit)
uv run bot.py

# Lint / format
uv run ruff check .
uv run ruff format .

# Type check
uv run pyright
```

Environment: copy `server/.env.example` to `server/.env` and fill in keys. Required: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`. Optional: `TTS_ENABLED=false` to disable TTS.

External dependency: `ttyd` and `tmux` must be installed on the system (`brew install ttyd tmux`).

## Architecture

The core pipeline lives in `bot.py` and is a Pipecat frame-processing pipeline:

```
transport.input()
  → DeepgramSTTService
  → user_aggregator
  → OpenAILLMService (GPT-4o with tool calls)
  → CockpitPrinter   (assembles token stream, logs, sends bot-transcription to browser UI)
  → CartesiaTTSService
  → transport.output()
  → assistant_aggregator
```

**AgentRouter** (`agent_router.py`): Wraps tmux. On client connect, creates/reattaches a tmux session named `cockpit` with a `fish` shell pane. LLM tool calls map to:
- `run_command` → `cd <dir> && <cmd>` sent to the tmux pane
- `send_input` → raw text sent to whatever is running
- `capture_output` → `tmux capture-pane` last N lines
- `find_directory` → walks `~` 3 levels deep with fuzzy matching

**ttyd proxy**: When a client connects, `bot.py` spawns `ttyd` on port 7681 attached to the tmux session. FastAPI proxies `/terminal/*` HTTP and `/terminal/ws` WebSocket to ttyd so the browser embeds a real terminal in `cockpit.html`.

**Cockpit UI** (`cockpit.html`): Single-page HTML/JS. Left panel: RTVI voice client (WebRTC, transcript bubbles, connect button). Right panel: ttyd iframe showing the tmux terminal. No build step — served directly as a static HTML file at `/cockpit`.

## Key Invariants

- The tmux session `cockpit` is created on client connect and killed on disconnect. `reset_session` (POST `/api/reset-terminal`) kills and recreates it.
- `CockpitPrinter` delays `LLMFullResponseEndFrame` until after it pushes the bot-transcription RTVI message, ensuring the browser renders text before firing `bot-llm-stopped`.
