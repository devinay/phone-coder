# Voice Coding Cockpit — Project Plan

## What This Is

A browser-based voice coding cockpit. The user speaks commands to an AI controller
(LLM) which executes them in a local terminal (tmux + fish shell). Extends into a
voice-driven documentation and diagramming system.

Run: `cd server && uv run bot.py` → open `http://localhost:7860/cockpit`

---

## Architecture

```
Browser (cockpit.html)
  ├── Left panel:  chat UI (WebRTC via Pipecat SmallWebRTC)
  └── Right panel: live terminal (ttyd iframe, proxied via /terminal)
  └── Doc overlay: markdown + diagrams (shown in doc_mode)
  └── Diagram focus overlay: single diagram fullscreen (shown in diagram_focus)

Bot server (bot.py, port 7860)
  ├── Deepgram STT      mic audio → text (diarization enabled)
  ├── LLM (switchable)  controller brain with tool use
  ├── TTS (switchable)  text → speech (Cartesia / OpenAI / Kokoro)
  └── ttyd proxy        /terminal + /terminal/ws proxied same-origin

State machines
  ├── DocStateMachine   shell ↔ doc_mode ↔ diagram_focus
  └── DiagramFocusStateMachine   idle → viewing → editing → saving → viewing

Storage: ~/voice-cockpit-docs/<slug>/version_N/
  ├── <slug>.md         main document
  ├── transcript.md     raw chronological conversation log
  └── speakers.json     speaker ID → name map
```

---

## What Has Been Built and Committed

### Voice Pipeline

| Feature | Status | Notes |
|---|---|---|
| Deepgram STT with diarization | ✅ Done | `diarize=True`, speaker ID extracted from word-level data |
| VAD-driven interruption | ✅ Done | `allow_interruptions=True` in PipelineParams |
| TTS toggle (server-side) | ✅ Done | `TTSGate` processor; default off; browser toggle sends `tts-toggle` message |
| TTS provider switching | ✅ Done | Cartesia / OpenAI TTS / Kokoro via `ServiceSwitcher` at runtime |
| LLM model dropdown | ✅ Done | OpenAI (gpt-4o-mini, gpt-4o, gpt-4.1-mini, gpt-4.1), Anthropic (haiku, sonnet, opus), Ollama (qwen2.5-coder:7b) |
| Cross-provider model switching | ✅ Done | Context reset on provider change; `set_full_model_name` for same-provider |
| Per-call LLM cost logging | ✅ Done | `LLMCallInspector` logs model, purpose, ~tokens, ~cost, cheaper alt to console |
| Fish path abbreviation fix | ✅ Done | `run_command` falls back to pane's current dir if path doesn't exist |

### Terminal Tools

| Tool | Status |
|---|---|
| `run_command(command, directory_path)` | ✅ Done — falls back gracefully on bad path |
| `send_input(text)` | ✅ Done |
| `capture_output(lines)` | ✅ Done |
| `find_directory(name)` | ✅ Done — fuzzy search 3 levels from ~ |

### Documentation Mode

| Feature | Status | Notes |
|---|---|---|
| `enter_doc_mode` / `exit_doc_mode` tools | ✅ Done | create or open project; saves on exit |
| `list_doc_projects` tool | ✅ Done | lists slugs + display names |
| `read_doc` tool | ✅ Done |
| `write_to_doc(content, section)` tool | ✅ Done | whole-doc or section replace; live browser update |
| Doc overlay in browser | ✅ Done | markdown rendered via marked.js |
| Transcript capture | ✅ Done | user + controller turns, chronological, written to transcript.md |
| Speaker naming (`set_speaker_name`) | ✅ Done | committed `a955652` |
| New-speaker detection | ✅ Done | `CockpitPrinter` detects unknown speaker ID → queues LLM turn asking for name |
| speaker_map pre-seeded | ✅ Done | `{"controller": "Controller", "0": "User"}` on `enter_doc_mode` |
| Atomic file writes | ✅ Done | write-then-rename via `atomic_write` |

### Diagramming (Phase 2)

| Feature | Status | Notes |
|---|---|---|
| `insert_diagram` tool | ✅ Done | Mermaid source in doc; syntax validated; unsupported types rejected |
| `update_diagram` tool | ✅ Done | replaces Mermaid block; sends live re-render event in focus mode |
| Mermaid CDN + rendering | ✅ Done | pinned `mermaid@11.4.1`, `securityLevel: strict`, dark theme |
| `enter_diagram_focus` tool | ✅ Done | transitions both state machines; injects scoped system prompt |
| `exit_diagram_focus` tool | ✅ Done | clears both state machines; restores doc view |
| `DiagramFocusStateMachine` | ✅ Done | `diagram_focus.py` — idle → viewing → editing → saving; separate from DocStateMachine |
| Focus overlay in browser | ✅ Done | full-screen diagram overlay with exit button |
| Live diagram re-render | ✅ Done | `diagram-focus-updated` event re-renders Mermaid in overlay on each `update_diagram` |
| Scoped controller in focus mode | ✅ Done | SYSTEM NOTE injected — controller refuses non-diagram commands |

---

## What Needs Manual Testing

Work through these in order. Each one depends on the previous.

### T1 — Basic voice pipeline (no doc mode)

1. `uv run bot.py`, open `http://localhost:7860/cockpit`, click Connect
2. Say "what directory are we in" → controller should call `run_command("pwd", ...)` and report the real path (not a fish-abbreviated path)
3. Say "list files here" → `ls` output should appear in the terminal and be summarised in the chat
4. Say "clear the screen" → terminal should clear without errors

**Pass criteria:** No `Error: Directory '...' does not exist` in logs. Controller reads actual `pwd` output.

---

### T2 — TTS toggle and model switching

1. Connect; click the voice toggle → controller replies should be spoken aloud (Kokoro or whichever provider is selected)
2. Speak while the controller is talking → speech should stop (interruption)
3. Use the model dropdown to switch from `gpt-4o-mini` to `claude-sonnet-4-6` mid-conversation → controller should continue naturally; logs should show `LLM switched to claude-sonnet-4-6 (provider change: openai→anthropic, context reset)`
4. Switch back to `gpt-4o-mini` → same

**Pass criteria:** No crash on model switch. Context reset log line appears on cross-provider switch. Voice interruption stops the current TTS stream.

---

### T3 — Doc mode basics

1. Say "enter documentation mode for testing speaker naming" → doc overlay should appear; project created at `~/voice-cockpit-docs/testing-speaker-naming/`
2. Have a short conversation with the controller (3–4 exchanges)
3. Say "write what we discussed to the document" → overlay should update with rendered markdown
4. Check `~/voice-cockpit-docs/testing-speaker-naming/version_0/transcript.md` → should contain both **User:** and **Controller:** turns in chronological order

**Pass criteria:** Overlay updates live. `transcript.md` has both speakers. `document.md` has content.

---

### T4 — Speaker naming (two speakers)

*Requires two people or passing the mic between two voices.*

1. Enter doc mode (any project)
2. Person A speaks → logged as `[USER speaker=0]: ...`
3. Person B speaks → logged as `[USER speaker=1]: ...` AND controller should interrupt and say "I heard a new voice — who is that?"
4. Reply with a name (e.g. "that's Bob") → controller calls `set_speaker_name("1", "Bob")`
5. Exit doc mode → check `speakers.json` → should contain `{"controller": "Controller", "0": "User", "1": "Bob"}`
6. Check `transcript.md` → Bob's lines should be labelled **Bob:**

**Pass criteria:** Controller asks for the name exactly once per new speaker ID. `speakers.json` written immediately (not just on exit).

**Known caveat:** Deepgram may not always return a `speaker` field at word level in streaming mode. If `[USER speaker=None]` appears in logs consistently, diarization is not working under live streaming — this is a known Deepgram streaming limitation to investigate separately.

---

### T5 — Diagram generation

1. Enter doc mode, say "create a sequence diagram for a user login flow"
2. Controller should call `insert_diagram` → diagram block appears in the overlay
3. Check `~/voice-cockpit-docs/<project>/version_0/<slug>.md` → should contain a ` ```mermaid\nsequenceDiagram ` block

**Pass criteria:** Diagram renders in the overlay. No JS console errors. File on disk matches what's shown.

---

### T6 — Diagram focus mode

1. After T5, say "enter diagram focus for \<diagram-id\>" (or the controller may suggest it automatically)
2. Doc overlay should hide; diagram focus overlay should appear fullscreen with the diagram rendered
3. Say "add an error handling step after the login" → controller rewrites Mermaid source, calls `update_diagram` → diagram should re-render live in the overlay
4. Say "exit diagram mode" (or click the ✕ Exit Focus button) → doc overlay should restore; diagram in the doc should reflect the edit

**Pass criteria:** Focus overlay activates cleanly. Live re-render works. Exit restores doc view. Updated diagram block in `document.md`.

---

### T7 — Regression check after doc mode

After any doc mode session, verify the coding flow still works:

1. Exit doc mode
2. Say "run the tests" or "list files in the current directory"
3. Controller should run the terminal command correctly — no stuck state, no "invalid state" errors

**Pass criteria:** Tool calls work normally after doc mode exit.

---

## What Is Not Built Yet

| Feature | Priority | Notes |
|---|---|---|
| Post-save diagram suggestion | Medium | Controller should suggest a diagram after `write_to_doc` — hook exists in prompt but not tested |
| Move diagram within doc | Medium | `"move diagram 1 into the introduction section"` — needs a `move_diagram` tool |
| Diagram numbered IDs | Low | Currently user-defined slugs; plan says `slug-N` format |
| Review Mode | Low | `review_mode` state defined but no tools implemented |
| Excalidraw integration (Phase 3) | Low | Needs Phase 2 fully verified first |
| Vision-assisted cleanup (Phase 5) | Low | Gate: Phases 3 + 4 complete |
| Speaker Merge utility | Low | For when diarization splits one voice into two IDs |
| `/api/state` status widget | Low | Live state inspector in cockpit header |
| Ollama (qwen2.5-coder:7b) tested | Blocked | Needs `ollama serve` + `ollama pull qwen2.5-coder:7b` to test |

---

## Known Issues

| Issue | Impact | Fix |
|---|---|---|
| `gpt-4o-mini` sometimes skips tool calls | Medium | Use `gpt-4.1` or `claude-sonnet-4-6` for tool-heavy sessions |
| Deepgram speaker field may be absent in streaming | Medium | Falls back to `speaker=None`; transcript labels as "User" for speaker 0 |
| `_diagram_focus_sm` is module-level but reset only on enter/exit — reconnect without clean exit may leave stale state | Low | Reconnect resets `_doc_sm` but not `_diagram_focus_sm`; fix: reset both on `on_client_disconnected` |
| `phonemizer` words count mismatch warning from Kokoro TTS | Cosmetic | Non-fatal; Kokoro still produces audio |

---

## File Map

| File | Role |
|---|---|
| `server/bot.py` | Pipeline, all LLM tools, FastAPI routes, TTS/LLM switching, state machine wiring |
| `server/agent_router.py` | tmux wrapper — run_command, send_input, capture_output, find_directory |
| `server/doc_state.py` | `DocStateMachine` — shell ↔ doc_mode ↔ diagram_focus transitions |
| `server/diagram_focus.py` | `DiagramFocusStateMachine` — inner states of a single diagram editing session |
| `server/doc_writer.py` | `AttributedUtterance` + `DocWriter` — accumulates utterances, renders transcript |
| `server/doc_storage.py` | Storage layer — create/load projects and versions, atomic write, path safety |
| `server/cockpit.html` | Browser UI — chat, terminal iframe, doc overlay, diagram focus overlay |

---

## Docs Storage Location

```
~/voice-cockpit-docs/
  <project-slug>/
    project.json
    version_0/
      <slug>.md         ← main document
      transcript.md     ← raw chronological log
      speakers.json     ← speaker ID → name
```

Override root: `VOICE_COCKPIT_DOCS_ROOT` env var.
