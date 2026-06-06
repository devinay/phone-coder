# TTS Toggle Plan

## Goal

Add an explicit browser toggle for spoken responses while keeping text responses always visible. The default mode must be text-only so Cartesia is not called unless the user enables voice.

## Current State

- `server/bot.py` currently has a `TTS_ENABLED` environment flag and conditionally inserts `CartesiaTTSService` into the Pipecat pipeline.
- `server/cockpit.html` already receives controller text via RTVI `bot-transcription` messages and renders it in the conversation pane.
- The browser has an audio element for bot audio, but it is currently muted.

## Design

1. Always keep the text response path active.
2. Keep Cartesia in the pipeline, but insert a small backend processor before it.
3. The processor marks LLM/TTS text frames with `skip_tts=True` whenever voice is disabled.
4. The browser sends a custom RTVI `client-message` when the toggle changes.
5. The backend updates runtime TTS state and sends an acknowledgement back to the UI.
6. If Cartesia emits an error, the backend switches TTS off and notifies the browser.

## Default Behavior

- TTS starts disabled.
- The browser toggle starts unchecked.
- The hidden audio element starts muted.
- Text output continues to show in the conversation pane.

## Error Fallback

If the Cartesia API errors while TTS is enabled:

- Log the error.
- Disable TTS for future responses.
- Send a browser event so the toggle returns to off.
- Keep text-only responses working.

## Files To Change

- `server/bot.py`
  - Add runtime TTS state.
  - Add a `TTSGate` processor before Cartesia.
  - Handle browser toggle messages through RTVI `on_client_message`.
  - Fall back to text-only on Cartesia `ErrorFrame`.

- `server/cockpit.html`
  - Add a voice toggle in the header.
  - Default it to off.
  - Send toggle changes to the backend through RTVI `client-message`.
  - React to backend TTS status messages.
  - Unmute the bot audio element only when voice is enabled.

## Implemented Summary

Implemented on 2026-06-02.

### Backend

- `server/bot.py` now defaults TTS to disabled with `TTS_ENABLED=false`, while still allowing `.env` to opt in with `TTS_ENABLED=true`.
- Added `TTSState` to hold runtime voice state.
- Added `TTSGate`, a `FrameProcessor` placed before `CartesiaTTSService`.
- `TTSGate` marks `TextFrame`, `LLMFullResponseStartFrame`, and `LLMFullResponseEndFrame` with `skip_tts=True` while voice is disabled.
- Cartesia remains in the pipeline, but should not receive billable text while voice mode is off.
- Added RTVI `on_client_message` handling for custom `tts-toggle` messages from the browser.
- Added backend `tts-status` server messages so the browser can sync its toggle state.
- If a Cartesia/TTS `ErrorFrame` is seen while voice is enabled, the backend disables TTS and tells the browser to return to text-only mode.

### Browser

- `server/cockpit.html` now has a header toggle labeled `voice off` / `voice on`.
- The toggle starts disabled until the WebRTC data channel opens.
- On connect, the browser explicitly sends `tts-toggle: false`, keeping startup in text-only mode.
- When the toggle changes, the browser sends an RTVI `client-message` with `t: "tts-toggle"` and `d: { enabled }`.
- The hidden bot audio element is muted while voice is off and unmuted while voice is on.
- The browser listens for `server-message` events with `type: "tts-status"` to update the toggle, including fallback after server-side TTS errors.

### Verification

- Ran `uv run python -m compileall server/bot.py`.
- Imported `server/bot.py` successfully with the project environment.
- Live browser/WebRTC behavior still needs a manual check by running `uv run bot.py`, opening `/cockpit`, connecting, and testing the toggle with a short prompt.

### Follow-Up Validation

Checked two suspected red flags against Pipecat 1.1.0:

- The browser sends RTVI `type: "client-message"` with nested `data.t = "tts-toggle"` and `data.d = { enabled }`. Pipecat's RTVI processor unwraps this into the event callback as `ClientMessage(type="tts-toggle", data={ enabled })`, so the server handler's `message.type == "tts-toggle"` check is correct.
- `skip_tts` is a real Pipecat field on `TextFrame`, `LLMFullResponseStartFrame`, and `LLMFullResponseEndFrame`. The base `TTSService` checks `frame.skip_tts` and forwards those frames without synthesis, so the gate should prevent Cartesia calls while voice is disabled.

Remaining caveat: Cartesia error fallback depends on Cartesia `ErrorFrame`s flowing back through `TTSGate`. The source suggests TTS services emit `ErrorFrame`s, but this path should still be confirmed with a live bad-key or forced-error test.
