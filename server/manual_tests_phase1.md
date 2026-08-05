# Phase 0 + Phase 1 Manual Test Plan

Run these after `uv run bot.py` and opening `http://localhost:7860/cockpit`.
Mark each test PASS / FAIL / SKIP and note any unexpected behaviour.

---

## Setup

```bash
uv run bot.py
# Open http://localhost:7860/cockpit
# Connect via the Connect button
```

Check `VOICE_COCKPIT_GIT_ROOT` is set in `.env` and points to an existing git
repository (e.g. `~/voice-cockpit-docs`). Initialize it first if needed:

```bash
mkdir -p ~/voice-cockpit-docs
git init ~/voice-cockpit-docs
```

Project directories are created automatically inside that repository.

---

## Section 1 — Bot startup (smoke tests)

| # | Test | How to verify | Result |
|---|---|---|---|
| 1.1 | Bot introduces itself on connect | Transcript bubble appears with intro message | |
| 1.2 | Terminal iframe shows live fish shell | Right panel shows a shell prompt | |
| 1.3 | TTS toggle starts off | Voice toggle in header is unchecked | |
| 1.4 | TTS toggle on/off round-trip | Flip toggle on → say something → flip off → no audio error | |

---

## Section 2 — Phase 0 browser PoCs (re-verify after bot.py changes)

| # | Test | URL | How to verify | Result |
|---|---|---|---|---|
| 2.1 | Mermaid security (all 3 auto-tests) | `/poc/poc3_mermaid_test` | All 3 show PASS ✅ | |
| 2.2 | Overlay autosave recovery | `/poc/poc5_overlay_test` | Enter doc mode → type → Autosave Now → Hard Refresh → 2 PASS ✅ badges | |
| 2.3 | Excalidraw iframe | `/poc/poc1_iframe_test` | Draw rectangle → Get Scene → PASS ✅ badge | |

---

## Section 3 — Happy path: doc mode round-trip

| # | Test | How to verify | Result |
|---|---|---|---|
| 3.1 | Create a new project | Say "start a new doc called test session" → bot confirms project created | |
| 3.2 | Directory structure created on disk | `ls "$VOICE_COCKPIT_GIT_ROOT/test-session"` → `test-session.md transcript.md speakers.json metadata.json diagrams/ artifacts/` all present | |
| 3.3 | Speak utterances in doc mode | Say 2–3 sentences → no error from bot | |
| 3.4 | Exit doc mode saves content | Say "exit doc mode" → bot confirms saved → `cat "$VOICE_COCKPIT_GIT_ROOT/test-session/test-session.md"` contains saved markdown | |
| 3.5 | Transcript contains utterances | `cat "$VOICE_COCKPIT_GIT_ROOT/test-session/transcript.md"` → your spoken lines appear with timestamps | |
| 3.6 | Shell restore after exit (Milestone 1.4) | Immediately after exit: say "run echo heartbeat" or type in terminal → shell responds, working directory unchanged | |
| 3.7 | Open existing project | Say "open the doc test session" → bot confirms project loaded, no new directory created | |

---

## Section 4 — Edge cases: project creation

| # | Test | How to verify | Result |
|---|---|---|---|
| 4.1 | Duplicate project name gets counter suffix | Say "start a new doc called test session" a second time (after exiting the first) → bot creates `test-session-1/`, original `test-session/` untouched | |
| 4.2 | Project name with special characters | Say "start a new doc called foo/bar..baz!" → project slug is sanitized (no `/`, `..`, `!`), directory created safely inside `VOICE_COCKPIT_GIT_ROOT` | |
| 4.3 | Path traversal attempt | Say "start a new doc called ../../etc/passwd" → bot creates a safe slug like `etcpasswd` or `untitled`, directory is inside `VOICE_COCKPIT_GIT_ROOT` | |
| 4.4 | Empty/meaningless project name | Say "start a new doc called !!!" → slug falls back to `untitled` or similar, project created without error | |

---

## Section 5 — Edge cases: state machine

| # | Test | How to verify | Result |
|---|---|---|---|
| 5.1 | enter_doc_mode is idempotent | While in doc mode, say "start a new doc called another one" → bot responds with ALREADY_ACTIVE, no second project directory created | |
| 5.2 | exit_doc_mode when not in doc mode | Say "exit doc mode" without ever entering it → bot responds with an error (INVALID_STATE or NOT_ACTIVE), no crash | |
| 5.3 | Re-enter doc mode after exit | Exit doc mode cleanly → immediately say "start a new doc called fresh start" → new project created, bot enters doc mode again | |
| 5.4 | State resets fully after exit | After a full round-trip, check that `_doc_sm.session.project_slug` is None (verify via log: bot should not mention the old project name on a new unrelated command) | |

---

## Section 6 — Edge cases: content and persistence

| # | Test | How to verify | Result |
|---|---|---|---|
| 6.1 | Exit with no utterances | Enter doc mode → immediately exit without speaking → `<slug>.md` exists and contains the expected document scaffold | |
| 6.2 | Exit with discard | Say "exit doc mode and discard" → bot calls `exit_doc_mode(discard=True)` → `<slug>.md` on disk remains unchanged | |
| 6.3 | Long utterance captured | Speak a long continuous sentence (10+ words) → verify it appears as a single bullet in the transcript, not truncated | |
| 6.4 | Single-user transcript assumption | Speak several turns in doc mode → `transcript.md` labels user/controller turns chronologically; no speaker-identification prompt appears | |
| 6.5 | Bot speech not captured as user utterance | While in doc mode, the bot's own spoken response must NOT appear as a user utterance in `<slug>.md` | |

---

## Section 7 — Edge cases: browser and connectivity

| # | Test | How to verify | Result |
|---|---|---|---|
| 7.1 | Browser hard-refresh while in doc mode | Enter doc mode → speak one sentence → hard-refresh the browser tab → reconnect → bot is back in shell state (no orphaned doc_mode session) | |
| 7.2 | Disconnect and reconnect | Enter doc mode → click Disconnect → click Connect again → bot starts fresh in shell state, old doc session is not partially active | |
| 7.3 | Bot restart while files exist | Stop `bot.py` → restart → say "open the doc test session" → existing project loads correctly | |

---

## Section 8 — Regression: existing coding commands

| # | Test | How to verify | Result |
|---|---|---|---|
| 8.1 | run_command still works | Say "run ls in my home directory" → bot runs the command and reads output | |
| 8.2 | find_directory still works | Say "find the documents directory" → bot returns a path | |
| 8.3 | send_input still works | Start an interactive program in the terminal, say "send the input hello" → input is sent | |
| 8.4 | Coding command after doc mode exit | Complete a full doc mode round-trip → then say "run git status in home" → bot executes normally with no regression | |

---

## Section 9 — Disk safety checks

Run these directly in the terminal, not via voice.

| # | Test | Command | Expected | Result |
|---|---|---|---|---|
| 9.1 | No files written outside git root | `find "$VOICE_COCKPIT_GIT_ROOT" -type f` | Only expected repo/project files | |
| 9.2 | No stale .tmp files after clean exit | `find "$VOICE_COCKPIT_GIT_ROOT" -name "*.tmp"` | Empty | |
| 9.3 | Markdown document is valid UTF-8 | `file "$VOICE_COCKPIT_GIT_ROOT/test-session/test-session.md"` | `UTF-8 Unicode text` | |
| 9.4 | metadata.json is valid JSON | `python3 -c "import json, os; json.load(open(os.path.join(os.environ['VOICE_COCKPIT_GIT_ROOT'], 'test-session', 'metadata.json')))"` | No error | |

---

## Section 10 — Agent runtime

Watch `server/logs/bot.log` while running these. Useful prefixes:
`[AGENT HANDOFF]`, `[AGENT SWITCH]`, `[AGENT TOOL]`, `[AGENT ADMIN]`,
`[AGENT PROMPT]`, and `[AGENT MODEL]`.

| # | Test | How to verify | Result |
|---|---|---|---|
| 10.1 | Controller starts as active agent | On cold start, ask "list agents" | Bot lists controller, shell, doc, diagram, image, web; log shows controller active | |
| 10.2 | Shell handoff returns to controller | Say "run echo heartbeat" | Logs show controller -> shell, `run_command`, then shell -> controller after final response | |
| 10.3 | Worker tool allowlist blocks wrong tool | While routed to shell, force or ask for a doc read | Tool result is `TOOL_NOT_ALLOWED`; underlying doc tool is not called | |
| 10.4 | Admin reload with context | Edit `server/agents/doc/prompt.md`, then say "admin reload doc prompt with context" | Bot says prompt reloaded; log shows `[AGENT PROMPT] reload_done`; context is preserved | |
| 10.5 | Reload without admin is denied | Say "reload doc prompt" without the `admin` prefix | Tool result is `ADMIN_REQUIRED`; prompt is not reloaded | |
| 10.6 | Diagram worker survives multi-tool loop | In doc mode, ask for a diagram edit that requires `enter_diagram_focus`, two `update_diagram` calls, and `exit_diagram_focus` | Agent remains diagram through tool calls; returns to controller only after final text | |
| 10.7 | Cross-provider model switch resets context | Switch GPT -> Claude from the UI | Log shows context reset; no provider-format crash | |
| 10.8 | Same-provider model switch can preserve context | Switch between two OpenAI models, then continue the same topic | Log shows same provider; recent conversation remains available | |
| 10.9 | Allowed controller-mediated composition works | Trigger a workflow that should route `doc -> diagram` or `diagram -> web` | Logs show `[AGENT COMPOSE] ... allowed=True` and then the expected handoff chain | |
| 10.10 | Forbidden composition is blocked cleanly | Trigger or force a flow equivalent to `doc -> shell` | Tool result is `COMPOSITION_NOT_ALLOWED`; log shows `[AGENT COMPOSE] ... allowed=False` with a reason | |
| 10.11 | Top-level image search works from a cold start | Say "find me an S3 icon" without entering diagram focus first | Controller routes to image; thumbnails appear; selecting one saves a path and clears the chooser | |
| 10.12 | Diagram/doc image embedding still works | In doc mode, focus a diagram and ask to replace a node with an icon | Existing `search_images -> select_image -> resize_image -> done_image` path still embeds into the document | |

---

## Phase 1 Gate — Go / No-Go

All items in Sections 3, 5, 6.1, 7.1, 8.4, and 10.2-10.12 must be PASS before starting
Phase 2 / Excalidraw.

| Gate check | Section | Result |
|---|---|---|
| Doc mode round-trip complete | 3.1–3.6 | |
| Shell restored after exit | 3.6 | |
| Idempotency enforced | 5.1 | |
| Invalid state rejected | 5.2 | |
| Empty session handled | 6.1 | |
| Browser refresh recovers cleanly | 7.1 | |
| No coding command regression | 8.4 | |
| Agent handoff returns cleanly | 10.2 | |
| Worker allowlist enforced | 10.3 | |
| Admin reload works and denial works | 10.4-10.5 | |
| Multi-tool diagram worker stays active | 10.6 | |
| Allowed composition chains work | 10.9 | |
| Forbidden composition chains are blocked | 10.10 | |
| Top-level image search works | 10.11 | |
| Diagram/doc image embedding still works | 10.12 | |

**Decision**: ⬜ Proceed to Phase 2 / ⬜ Fix issues first
