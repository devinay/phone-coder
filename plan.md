# Voice Coding Cockpit

## Architecture

A voice-driven coding cockpit running entirely on localhost.

```
Browser (cockpit.html)
  ├── Left panel: chat UI (WebRTC via Pipecat SmallWebRTC)
  └── Right panel: live terminal (ttyd iframe, proxied via /terminal)

Bot server (bot.py, port 7860)
  ├── Deepgram STT  — mic audio → text
  ├── GPT-4o LLM   — controller brain with tool use
  ├── Cartesia TTS  — text → speech (optional, gracefully degrades)
  └── ttyd proxy    — /terminal + /terminal/ws proxied same-origin to avoid Chrome PNA block

Terminal (agent_router.py)
  └── One tmux session ("cockpit") with one fish shell window ("shell")
      Started on client connect, killed on disconnect
      ttyd attaches to this session and streams it to the browser
```

## Lifecycle

1. `uv run bot.py` — starts the bot server
2. Open `http://localhost:7860/cockpit`
3. Click **Connect** → mic access granted, WebRTC connects, tmux session + ttyd start, terminal iframe loads
4. Speak or type to the controller → LLM routes commands to the fish shell via tmux send-keys
5. Click **Disconnect** → ttyd killed, tmux session killed (fish + all subprocesses die)

## Controller Tools

| Tool | Description |
|---|---|
| `run_command(command, directory_path)` | cd to directory and run any shell command (start claude, git, etc.) |
| `send_input(text)` | send raw text to whatever is running in the terminal |
| `capture_output(lines)` | read recent terminal output |
| `find_directory(name)` | fuzzy-search for a directory up to 3 levels deep from ~ |
| `set_language(language)` | switch STT mode (planned — see below) |

## Files

| File | Role |
|---|---|
| `server/bot.py` | Pipeline, tools, FastAPI routes, ttyd proxy |
| `server/agent_router.py` | tmux wrapper (one session, one fish shell) |
| `server/cockpit.html` | Browser UI (chat + terminal iframe) |

---

## Planned: Dual-Mode STT (multilingual support)

### Motivation
Deepgram supports English well (low latency, streaming) but not South Indian languages.
Whisper supports Kannada, Telugu, Tamil, Hindi but is batch (higher latency).
Goal: keep Deepgram for English, use Whisper for non-English — switchable at runtime via voice.

### Pipeline change
```
transport.input()
→ WhisperSideCar      new: taps audio, runs VAD, calls Whisper on pause in non-English mode
→ DeepgramSTTService  unchanged, always connected
→ LanguageGate        new: drops Deepgram TranscriptionFrames in non-English mode
→ user_aggregator → llm → ...
```

### WhisperSideCar (new processor)
- Always passes AudioRawFrame through so Deepgram gets audio untouched
- Runs SileroVADAnalyzer internally on every frame
- In non-English mode: accumulates audio while speaking; on VAD pause → encodes PCM to WAV → calls OpenAI Whisper API with language code → pushes `WhisperTranscriptionFrame` downstream
- In English mode: does nothing with audio

### LanguageGate (new processor)
- In English mode: passes all frames through
- In non-English mode: drops `TranscriptionFrame` (Deepgram output), passes `WhisperTranscriptionFrame` (subclass — user_aggregator's isinstance check still matches)

### Switching
- User says **"I am switching to Kannada"** (in English) → Deepgram transcribes → LLM calls `set_language("kannada")`
- User says **"switch back to English"** (in any language — Whisper transcribes intent) → LLM calls `set_language("english")`
- LLM recognises switch intent regardless of phrasing or language (system prompt instructs this)
- All terminal tools (run_command, send_input, etc.) work in any language mode

### Language codes
| Language | Whisper code |
|---|---|
| Kannada | `kn` |
| Telugu | `te` |
| Tamil | `ta` |
| Hindi | `hi` |
