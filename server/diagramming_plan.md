# Voice-Driven Documentation and Diagramming Plan

## Overview

Extend the Voice Coding Cockpit with a Documentation Mode that supports voice-driven markdown editing and interactive diagramming. The user speaks commands to create, edit, and refine diagrams alongside documentation — all within the existing browser UI.

This is the definitive consolidated plan, incorporating all insights from independent Codex and Gemini architecture reviews. It is organized into four parts: Feature Specifications, Architecture, Cross-Cutting Concerns, and Delivery Phases.

---

## Part 1 — Feature Specifications

### Documentation Mode

#### Activation

##### Trigger Phrases

| Phrase | Behaviour |
|--------|-----------|
| `"enter documentation mode"` | Agent asks: *"Do you want to open an existing document or create a new one?"* |
| `"enter documentation mode for <name>"` | Agent immediately creates a new project named `<name>` — no follow-up question. |
| `"exit documentation mode"` | Agent saves and closes Documentation Mode, restores the terminal shell. |
| `"exit documentation mode and discard"` | Agent exits without saving the current session content. |

Only these exact phrases trigger the mode transitions. Other phrasing (e.g. *"let's document this"*) will not call `enter_doc_mode` or `exit_doc_mode`.

- A new browser pane overlays the terminal shell when Documentation Mode is entered.
- On entry without an inline name, the agent prompts: *"Do you want to open an existing document or create a new one?"*

##### Open Existing Document
- The agent lists available projects from `VOICE_COCKPIT_DOCS_ROOT`.
- After project selection, the agent lists available versions and defaults to the current/latest version from `project.json`.
- The selected version's `document.md` is loaded into the editor, ready for continued discussion or diagram editing.

##### Create New Document
- The agent prompts for a project/topic name.
- A sanitized project directory is created under `VOICE_COCKPIT_DOCS_ROOT`.
- A new `version_0/document.md` file is created and the session begins capturing discussion.

#### Markdown Interaction
- The voice agent engages in a discussion and updates the markdown file based on the conversation.
- The agent identifies spots where diagrams would clarify content and inserts placeholders automatically.

#### Diagram Workflow
1. For each placeholder, the agent prompts: *"Do you want to make changes to diagram one, two, etc.?"*
2. The markdown view closes and a full-screen diagram editor opens for that diagram.
3. The user edits the diagram via voice commands and mouse interactions in real time.
4. On save, the updated diagram is reintegrated into the markdown.
5. The process repeats for each diagram.

#### Documentation Output

A single markdown file is produced at the end of (or during) a session, structured in two parts:

1. **Main Content** — the primary discussion, decisions, and embedded diagrams, synthesized by the AI agent into coherent prose. Does not include raw transcript lines.
2. **Transcript Section** — a detailed per-speaker record appended at the end, with each speaker's own subsection recording their ideas, suggestions, and diagram edits.

```markdown
# Session Title

## Main Content

[Primary discussion, decisions, and action items written by the AI agent]

### Diagram: Auth Flow
```mermaid
sequenceDiagram
...
```

---

## Transcript

### Speaker 1 (Alice)
- Suggested starting the auth flow from the login page rather than the landing page.
- Requested update to the sequence diagram to add the OAuth callback step.

### Speaker 2 (Bob)
- Agreed with Alice's auth flow proposal.
- Added error handling branch to the sequence diagram.
```

#### Exit
- The markdown is finalized with all edits and diagrams included.
- The overlay closes and the terminal shell is restored.

---

### Review Mode

#### Overview

Review Mode allows one or more participants to open an existing project version, discuss its contents, and record comments, approvals, disagreements, and agreed changes — all attributed by speaker. At the end of the session the review is saved as a new `version_N` directory.

#### Entering Review Mode
- The agent presents a list of projects and versions available for review:
  - Newly created projects and versions from the current session
  - Existing projects under `VOICE_COCKPIT_DOCS_ROOT`
  - Existing project versions such as `version_0`, `version_1`, and the current/latest version
- The selected version's `document.md` is opened in the review pane.

#### Editing and Interaction During Review
- Reviewers can modify document content via voice commands.
- Diagrams can be edited using both voice commands and mouse interactions (Excalidraw).
- The agent tags each change with the speaker's name in the Review section.
- Supported interaction types:
  - **Comment**: *"Speaker 1 (Alice): Suggested rephrasing the intro paragraph."*
  - **Edit**: *"Speaker 2 (Bob): Updated the sequence diagram to add the logout step."*
  - **Approval**: *"Speaker 1 (Alice): Approved the updated auth flow diagram."*
  - **Disagreement**: *"Speaker 2 (Bob): Disagreed with the error handling approach — prefers a retry loop."*

#### Review Section in the Document

A `## Review` section is appended to the document during the review session:

```markdown
## Review

**Date**: 2026-06-06
**Participants**: Alice (Speaker 1), Bob (Speaker 2)

### Comments and Changes

- **Speaker 1 (Alice)**: Suggested rephrasing the introduction for clarity.
- **Speaker 2 (Bob)**: Updated the sequence diagram — added logout step.
- **Speaker 1 (Alice)**: Approved the updated auth flow diagram.
- **Speaker 2 (Bob)**: Disagreed with the error handling approach; proposed a retry loop instead.

### Agreed Changes
- Auth flow diagram updated to include OAuth callback and logout steps.
- Error handling approach to be revisited in the next session.
```

---

### Versioning and Storage

All documentation projects are stored under `VOICE_COCKPIT_DOCS_ROOT`. Each project gets a sanitized directory. New sessions start in `version_0`; each review or substantial iteration creates a new `version_N` directory. Historical version directories are never mutated — editing an older version creates a new version derived from it.

```text
<cockpit_docs_root>/
  ABC/
    project.json
    version_0/
      manifest.json
      document.md
      transcript.md
      speakers.json
      diagrams/
      artifacts/
    version_1/
      manifest.json
      document.md
      transcript.md
      review.md
      speakers.json
      diagrams/
      artifacts/
```

#### Metadata Requirements

- `project.json`: project display name, sanitized slug, creation timestamp, current version, version list.
- `manifest.json` (per version): version number, `derived_from`, created timestamp, document/transcript/review/speaker map paths, diagram list, artifact list. All paths relative to the project directory.
- Filesystem dates are convenience metadata only; explicit metadata is the source of truth for audit and version lineage.
- Version creation uses atomic directory creation with a project-level lock or create-and-retry flow to avoid duplicate version numbers.

---

## Part 2 — Architecture

### Library Selection

#### Chosen Stack: Excalidraw + Mermaid.js

After evaluating Fabric.js, JointJS, Rough.js, Two.js, tldraw, and others, the recommended combination is Excalidraw for freehand editing and Mermaid.js for AI-generated structured diagrams.

#### Library Decision: Excalidraw vs tldraw (evaluated 2026-06-08)

tldraw was considered as an alternative to Excalidraw, specifically because of:
- The **tldraw "Make Real"** pattern — sketch on canvas → screenshot → vision model → polished output, which is a documented, first-class integration point
- Built-in support for **`perfect-freehand`** (the stroke smoothing library by the same author, Steve Ruizok), which gives natural pressure-sensitive strokes

**Why Excalidraw wins for this project:**

1. **Freehand is already built into Excalidraw** — it has a native pencil/freedraw tool with stroke smoothing comparable to `perfect-freehand`. There is no capability gap; `perfect-freehand` does not need to be added separately.

2. **The Mermaid bridge exists for Excalidraw** — `@excalidraw/mermaid-to-excalidraw` converts Mermaid source directly to editable Excalidraw `ExcalidrawElement[]` objects with positions and shapes. This means an AI-generated Mermaid diagram can be escalated to the Excalidraw canvas as fully editable vector elements, not a flat image. No equivalent bridge exists for tldraw.

3. **tldraw SVG import is a flat image** — importing a Mermaid-rendered SVG into tldraw produces a single locked image element. The user cannot edit internal nodes or edges. This makes it unsuitable for the "edit a Mermaid diagram manually" use case.

4. **"Make Real" is not a tldraw exclusive** — the vision model integration (canvas screenshot → Claude Sonnet → structured output) is something we build ourselves regardless of which canvas library we use. It is not a reason to prefer tldraw.

**tldraw remains the stated fallback** if the Excalidraw iframe PoC (Phase 0) fails browser verification. In that case, evaluate tldraw before starting Phase 3, accepting the loss of the Mermaid bridge.

**The two tools serve parallel, non-overlapping roles — do not replace one with the other:**

```
Voice command → controller LLM → Mermaid text → SVG in doc overlay   (AI generates structure)
User draws / annotates           → Excalidraw canvas in iframe         (user edits manually)
Mermaid diagram escalated        → @excalidraw/mermaid-to-excalidraw   (bridge between the two)
```

#### Excalidraw
- Handles freehand drawing (native pencil tool, stroke smoothing built in), shape manipulation, and text annotations.
- Outputs a structured JSON scene format (`ExcalidrawElement[]`) — readable and rewritable by the LLM.
- Supports SVG export.
- Best fit for: *user draws or annotates → AI cleans up via vision model*.
- **Integration strategy**: Excalidraw requires React and cannot be safely CDN-loaded into `cockpit.html` without dependency isolation. It is embedded in a dedicated `editor.html` iframe sandbox. This prevents CSS/JS collisions and React version conflicts with the main cockpit page. `cockpit.html` communicates with the iframe via `postMessage`. The Phase 0 PoC must validate the full editor lifecycle before any production code is written; if the iframe approach fails, `tldraw` is the fallback.

#### Mermaid.js
- Generates structured diagrams (flowcharts, sequence diagrams, Gantt charts, and more) from text.
- CDN-friendly, no build step required — compatible with the current no-build `cockpit.html` architecture.
- Best fit for: *AI generates diagram from description*.
- **Security requirement**: Mermaid source is generated from user speech and LLM output and is untrusted input. Configure `securityLevel: 'strict'` or `'sandbox'`, pin the CDN version, validate syntax before writing to the document, and preserve the last valid source for rollback. Never pass Mermaid label text back to the agent as instructions.

#### Mermaid Compatibility Matrix

The LLM must only generate diagram types that Mermaid supports and that the system has validated. If the LLM selects an unsupported type, the agent notifies the user via voice and falls back to the closest supported type or a plain flowchart.

| Diagram type | Mermaid keyword | Supported | Notes |
|---|---|---|---|
| Flowchart | `flowchart` / `graph` | Yes | Default for general-purpose diagrams |
| Sequence diagram | `sequenceDiagram` | Yes | Best fit for API and interaction flows |
| Class diagram | `classDiagram` | Yes | Best fit for data model documentation |
| State diagram | `stateDiagram-v2` | Yes | Best fit for mode/state machines |
| Entity-relationship | `erDiagram` | Yes | Best fit for database schemas |
| Gantt chart | `gantt` | Yes | Best fit for timelines and schedules |
| Pie chart | `pie` | Yes | Simple proportional data only |
| Git graph | `gitGraph` | Yes | Best fit for branching visualizations |
| Timeline | `timeline` | Yes | Linear event sequences |
| Mind map | `mindmap` | Yes | Hierarchical topic exploration |
| Bar / XY chart | `xychart-beta` | Limited | Beta status; avoid for production docs |
| Data-backed charts (scales, interactivity) | — | No | Use Vega-Lite or Chart.js in a future phase |

**Fallback behavior**: When the LLM generates a type marked Limited or No, the system rejects the source, notifies the user (*"That diagram type isn't supported yet — I'll use a flowchart instead"*), and re-prompts the LLM with the constraint.

#### Diagram Storage

- **Mermaid diagrams**: stored as fenced code blocks (` ```mermaid `) in `document.md`.
- **Excalidraw diagrams**: stored as sidecar `.excalidraw` files in `version_N/diagrams/`. `document.md` references the sidecar by diagram ID, keeping the primary markdown clean for LLM processing and avoiding large embedded JSON.
- The original Mermaid source is always preserved when a diagram is escalated to Excalidraw — never discarded.

#### Why Not All Libraries?
Stacking multiple libraries causes bundle bloat (5–10 MB+), overlapping canvas ownership conflicts, and multiple incompatible data models for the AI to reconcile. A focused stack is easier to maintain, version, and extend.

---

### Model Configuration

Model availability and capabilities change over time. Rather than hardcoding a single model, the system uses named capability slots:

| Capability slot | Recommended model | Rationale |
|---|---|---|
| `diagram_cleanup_model` | Claude 3.5 Sonnet | Strongest spatial reasoning and valid Mermaid/Excalidraw generation from visual input |
| `vision_model` | Claude 3.5 Sonnet | Best-in-class for interpreting pixel layout alongside JSON structure |
| `fast_validation_model` | Gemini 1.5 Flash | Near-instant latency, low cost — ideal for background readability checks |
| `summarization_model` | GPT-4o-mini or Gemini 1.5 Flash | Cost-effective for transcript and main-content synthesis |
| `fallback_text_model` | Any text-only model | Used when vision is unavailable or user has not opted in |

Each slot is overridable per environment (development, production, cost-sensitive sessions). Required capability flags per slot: `image_input`, `structured_json_output`, `low_latency`.

#### Task-Specific Routing

- **Summarization / transcript synthesis**: `summarization_model`
- **Complex diagram cleanup and layout**: `diagram_cleanup_model`
- **Spatial interpretation of freehand sketches**: `vision_model`
- **Real-time background validation** (e.g., "Is this diagram readable?"): `fast_validation_model`
- **Text-only diagram edits and JSON-only cleanup** (Mermaid source rewrite via voice, scene JSON rewrite without vision): primary reasoning model in the existing pipeline

---

### Integration Model

#### Diagram-as-Code First Strategy

Diagrams exist in two representations with explicit escalation rules:

- **State 1 — Pure Mermaid (default)**: As long as the user issues logic-based commands ("Add a step", "Change this label"), the diagram stays as Mermaid text. It is faster, cheaper, and more reliable for the AI to edit.
- **State 2 — Excalidraw artifact**: The moment the user requests manual visual refinement ("Move this box", "Draw a freehand annotation"), the system escalates to Excalidraw JSON. The original Mermaid source is preserved alongside the `.excalidraw` sidecar.
- **The bridge**: Use `@excalidraw/mermaid-to-excalidraw` to convert Mermaid source directly to Excalidraw elements. SVG import is a fallback only — converting SVG output to editable primitives produces lower-quality results than parsing Mermaid syntax.
- **Reverse bridge**: Use `diagram_cleanup_model` (Claude 3.5 Sonnet) to interpret manual Excalidraw sketches back into Mermaid logic when the user asks for a structured export.

#### Dual-Mode Diagram Flow

```
[User describes diagram via voice]
  → LLM generates Mermaid text
  → Validate syntax; render to SVG via pinned Mermaid CDN
  → Present in doc view; offer Focus Mode
  → [If user requests visual refinement]
      → @excalidraw/mermaid-to-excalidraw conversion
      → Open Excalidraw in editor.html iframe Focus Mode
      → User refines freehand or via voice commands
      → Save Excalidraw JSON as sidecar .excalidraw file
      → Preserve original Mermaid source alongside it
```

#### JSON-Only AI Cleanup Flow (Phase 3, no vision required)

```
[User voice command: "clean this up" / "rearrange" / "simplify"]
  → Serialize current Excalidraw scene JSON
  → Send to fallback_text_model with cleanup prompt
  → Validate returned JSON against Excalidraw schema
  → Keep rollback snapshot of previous scene
  → Apply updated JSON to Excalidraw canvas
```

#### Vision-Assisted AI Cleanup Flow (Phase 5, opt-in)

```
[User voice command: "clean this up" — vision opt-in active]
  → Capture Excalidraw canvas as compressed PNG
  → Send to diagram_cleanup_model (Claude 3.5 Sonnet):
       - image: compressed PNG screenshot
       - text: user's voice command + current Excalidraw scene JSON
  → Validate returned JSON against Excalidraw schema
  → Show user-visible diff: highlight added/removed/moved elements
  → Voice prompt: "Here's what I changed — shall I apply it?"
  → On confirmation, write updated JSON to sidecar file
  → Keep rollback snapshot; restore automatically if JSON is invalid
  [Fallback if vision unavailable or not opted in]
  → JSON-only cleanup via fallback_text_model
```

#### Context Window Pruning

When in `diagram_focus` mode, the LLM's context is strictly limited to the active diagram source (Mermaid text or Excalidraw JSON) and the relevant conversation window (last N turns). The full document markdown and unrelated transcript history are excluded to control latency and cost.

---

### Mode State Machine

Mode changes are implemented as explicit state transitions shared by the server controller and browser UI. This prevents duplicate sessions, orphaned overlays, and accidental writes to the wrong project version.

#### States

- `shell`: Default terminal/coding mode.
- `doc_mode`: Documentation editor overlay is active.
- `diagram_focus`: A single diagram is open for fullscreen editing (Mermaid or Excalidraw in iframe).
- `review_mode`: Review pane is active for a selected project version.
- `saving`: A document, diagram, or review version is being written.
- `error_recovery`: A failed transition or write is being recovered without losing the last valid state.

#### Transition Rules

| Tool call / event | Allowed source state | Resulting state | Notes |
|---|---|---|---|
| `enter_doc_mode` | `shell` | `doc_mode` | Creates or opens a project and version directory. |
| `enter_doc_mode` | `doc_mode` | `doc_mode` | Idempotent no-op; must not create duplicate files. |
| `exit_doc_mode` | `doc_mode` | `saving` → `shell` | Saves current version, clears overlay, restores shell. |
| `enter_diagram_focus` | `doc_mode` | `diagram_focus` | Requires a selected diagram ID; opens Mermaid or Excalidraw editor in iframe. |
| `exit_diagram_focus` | `diagram_focus` | `saving` → `doc_mode` | Saves or discards diagram edits, then restores doc overlay. |
| `enter_review_mode` | `shell` | `review_mode` | Lists projects and versions from `VOICE_COCKPIT_DOCS_ROOT`. |
| `exit_review_mode` | `review_mode` | `saving` → `shell` | Creates a new `version_N` directory if review output changed. |
| Browser refresh | `doc_mode`, `diagram_focus`, `review_mode` | `error_recovery` → restored state or `shell` | Server pushes autosave state back to client; clears orphaned overlay. |
| Save failure | `saving` | `error_recovery` | Preserves the previous valid document, diagram, and version metadata via rollback snapshot. |

#### State Contract Requirements

- Every mode tool call declares its allowed source states, resulting state, idempotency behavior, browser event emitted, server-side mutation performed, and failure behavior.
- The server is authoritative for state; the browser reflects state changes via events.
- The current project slug, selected version, selected diagram ID, and current speaker map are all part of session state.
- No browser overlay should remain visible after a transition back to `shell`.
- No file write should occur without a selected project/version context.

#### Browser UI

- The Documentation Mode overlay is a JS panel within `cockpit.html` — no new routes needed.
- Adopt a strict **State-Reducer** pattern to manage all overlay transitions. Avoid ad-hoc imperative overlay toggling, which produces spaghetti state.
- The Excalidraw editor lives in `editor.html` iframe; `cockpit.html` communicates with it via `postMessage`.

---

### Speaker Attribution

#### Normalized STT Events

`DocWriter` must not depend directly on provider-specific frame structure. An adapter layer converts raw Deepgram frames into stable documentation events before `DocWriter` consumes them:

```python
@dataclass
class AttributedUtterance:
    text: str
    timestamp: float
    speaker_id: str | None       # Deepgram speaker ID, if available
    confidence: float | None     # Attribution confidence, if available
    fallback_label: str          # "Speaker Unknown" when attribution missing
    raw_payload: dict            # Original provider payload for debugging
```

#### Advisory Attribution Policy

- Speaker attribution is treated as advisory, not authoritative.
- Raw transcript entries with timestamps are always preserved for audit.
- Uncertain attribution is marked explicitly; contributions are never silently dropped.
- Do not prompt for speaker names during active overlapping speech — defer to post-capture correction.
- Allow manual correction of speaker names and attribution after capture.
- Persist the Deepgram speaker ID separately from the human-readable name in `speakers.json`.

#### Speaker Identification Flow

- Deepgram's diarization assigns speaker IDs (e.g., `Speaker 0`, `Speaker 1`) to each voice segment. Enable `diarize: true` in `DeepgramSTTService`. Verify that `TranscriptionFrame` carries a reliable `speaker` field under live streaming conditions — behavior differs from batch diarization.
- When a new speaker ID is detected for the first time, the agent prompts: *"I've detected a new speaker. What is their name?"*
- The provided name is stored in a session-level speaker map (`{ "0": "Alice", "1": "Bob" }`) and used for all subsequent attribution.
- The speaker map persists as `speakers.json` inside the active version directory for session continuity.

#### Speaker Merge Utility

Diarization frequently fragments one person into two IDs (e.g., Speaker 0 and Speaker 2) if they move or the environment changes. Review Mode exposes a **Speaker Merge** action that lets reviewers combine two IDs into one speaker and re-attribute their contributions retroactively.

---

### Tool-Call Contracts

All mode-transition and document-mutation tool calls must have defined request/response schemas before implementation begins.

| Tool | Required fields | Optional fields | Error responses |
|---|---|---|---|
| `enter_doc_mode` | `action` (open/create), `project_slug` or `topic_name` | `version` | `ALREADY_ACTIVE`, `PROJECT_NOT_FOUND`, `WRITE_ERROR` |
| `exit_doc_mode` | — | `discard` | `SAVE_FAILED`, `NOT_ACTIVE` |
| `insert_diagram` | `diagram_id`, `diagram_type` (mermaid/excalidraw), `source` | `position_hint` | `INVALID_SYNTAX`, `ID_COLLISION` |
| `update_diagram` | `diagram_id`, `source` | — | `INVALID_SYNTAX`, `ID_NOT_FOUND`, `SAVE_FAILED` |
| `enter_diagram_focus` | `diagram_id` | — | `ID_NOT_FOUND`, `INVALID_STATE` |
| `exit_diagram_focus` | — | `discard` | `SAVE_FAILED`, `NOT_ACTIVE` |
| `enter_review_mode` | `project_slug`, `version` | — | `VERSION_NOT_FOUND`, `ALREADY_ACTIVE` |
| `append_review_entry` | `entry_type` (comment/edit/approval/disagreement), `speaker_id`, `text` | `diagram_id` | `NOT_IN_REVIEW` |
| `save_review_version` | — | — | `SAVE_FAILED`, `VERSION_COLLISION` |
| `clean_diagram` | `diagram_id`, `scene_json` | `image_base64` | `INVALID_JSON`, `MODEL_ERROR`, `VISION_UNAVAILABLE` |

Each contract must declare: allowed source states, resulting state, idempotency behavior, browser event emitted, server-side mutation performed, and failure behavior.

---

## Part 3 — Cross-Cutting Concerns

### Persistence and Atomicity

#### Atomic Write Protocol

All file writes follow a write-then-rename pattern to prevent partial writes:

1. Write content to a temporary file (e.g., `document.md.tmp`) in the same directory.
2. Atomically rename the temp file to the final path.
3. Update `manifest.json` only after the primary file write succeeds.
4. Never overwrite user-authored files without creating a new version first.
5. Detect external file modifications (via mtime or hash) before saving; warn if the file changed outside the session.

#### Autosave and Session Affinity

- Session state (active document, diagram sources, speaker map) is autosaved separately from the final committed markdown, targeting a `.session/` directory inside the active version directory.
- On browser refresh or socket reconnect, `bot.py` reads autosave state and pushes it back to the client (server-side session affinity). This prevents data loss on accidental reloads.
- Autosave does not create a new `version_N` directory — only explicit `exit_doc_mode` or `save_review_version` does.

#### Rollback Snapshots

- A rollback snapshot of the previous scene JSON is kept before every AI diagram edit.
- A rollback snapshot of the previous Mermaid source is kept before every LLM rewrite.
- If the AI returns invalid JSON or syntax, the rollback is applied automatically and the user is notified via voice.

---

### Path and Workspace Safety

- Resolve all paths to absolute canonical paths before any file operation.
- Prevent path traversal: sanitize voice-derived project names and diagram IDs (strip `..`, `/`, and shell metacharacters).
- Confirm the target directory is under `VOICE_COCKPIT_DOCS_ROOT` before writing.
- Confirm the target directory is writable at session start; surface a clear error if not.
- Handle duplicate sanitized filenames deterministically (append a counter suffix).
- Never write outside the expected workspace unless the user has explicitly configured an alternate root.

---

### Prompt Injection and Content Trust

The system ingests markdown, Mermaid source, Excalidraw JSON, transcript text, and optionally web search results. All of these can contain prompt-injection content.

- Treat document contents as data, not instructions to the agent.
- Keep system prompts structurally separate from reviewed document text — never interpolate raw document content directly into the system prompt.
- Do not let embedded Mermaid labels or markdown comments modify agent behavior.
- Sanitize web search snippets before summarization; require confirmation before inserting externally sourced claims into the document.
- Never send a diagram to a vision model without explicit user opt-in (diagrams may contain proprietary content). Apply the same opt-in policy to web search.

---

### Observability

The following events must be logged for debugging, cost tracking, and audit:

- Mode transitions (source state → resulting state, tool call, timestamp)
- File save paths and outcomes (success, failure, rollback)
- Diagram IDs and edit operations (insert, update, AI cleanup, rollback)
- Mermaid render failures (source excerpt, error message)
- Speaker mapping events (new speaker detected, name assigned, merge performed)
- STT attribution fallback events (missing speaker field, confidence below threshold)
- Model used for each cleanup or vision call
- Latency and input size (token count, image bytes) for all multimodal calls
- Version creation events (new `version_N` directory, `derived_from` pointer)

---

## Part 4 — Delivery Phases

### Phase Ordering Rationale

The phases follow this sequence: infrastructure validation → foundational backend → Mermaid diagrams and UI → Excalidraw with JSON-only AI cleanup → Review Mode → vision-assisted AI cleanup.

Each phase must fully pass its go/no-go gate before the next phase begins. A phase fails its gate if any milestone marked **PASS** produces an incorrect result, any crash or data-loss scenario occurs during an edge-case test, or the shell restore check fails.

Visual AI (Phase 5) is intentionally last. The foundations — `DocWriter`, normalized STT events, atomic persistence, the state machine, the speaker map — are prerequisites for every subsequent phase. Building vision AI on an unstable foundation forces rework. Phase 0 PoC 6 validates the AI model stack early as a spike; Phase 5 builds on it once the rest is stable. Vision API calls are also expensive and gated behind explicit user opt-in; deferring keeps development costs predictable.

---

## Implementation Status (updated 2026-06-08, commit 62aa65c)

| Phase | Status | Notes |
|---|---|---|
| Phase 0 | ✅ PASS (2026-06-06) | PoC 1/3/5 browser verified PASS. PoC 4: 16/16 pytest. PoC 6: PASS (skips without API keys). PoC 2: non-blocking; verify with live two-speaker session. |
| Phase 1 | ✅ PASS (2026-06-07) | 44/44 pytest pass. `enter_doc_mode` / `exit_doc_mode` wired. `TranscriptionFrame` feeds `DocWriter`. State-Reducer scaffold in cockpit.html. Milestone 1.4 shell-restore: live-verified PASS. |
| Phase 2 | 🔧 Implemented — pending live verification | All tools wired (`insert_diagram`, `update_diagram`, `enter_diagram_focus`, `exit_diagram_focus`). `DiagramFocusStateMachine` added (`diagram_focus.py`). Scoped system prompt on focus enter. Live re-render via `diagram-focus-updated`. Exit button in overlay. **Milestones 2.1–2.4 require live browser verification before Phase 3 can start.** |
| Phase 3 | ⬜ Not started | Gate: Phase 2 milestones 2.1–2.4 all PASS |
| Phase 4 | ⬜ Not started | Gate: Phase 3 all PASS |
| Phase 5 | ⬜ Not started | Gate: Phase 4 all PASS |

Rollback tag: `phase0-pre` (pre-Phase-0 clean state on `main`).

---

## What's New Since Last Plan Update (commits `a955652`, `e5e45ff`, `62aa65c`)

### Speaker Naming — commit `a955652`

**What was built:**
- `set_speaker_name(speaker_id, name)` tool — assigns a human-readable name to a Deepgram speaker ID during an active doc session; updates `speaker_map`, retroactively relabels all past `DocWriter` utterances, and writes `speakers.json` immediately (not just on exit)
- `CockpitPrinter` now holds a `task`/`context` reference via `set_task_context()` — when a `TranscriptionFrame` arrives in `doc_mode` with an unknown speaker ID, it injects a `[SYSTEM NOTE]` into the LLM context and queues an `LLMRunFrame` to prompt the controller to ask who the new speaker is
- `_asked_speakers` set prevents re-asking for the same ID
- `speaker_map` pre-seeded with `{"controller": "Controller", "0": "User"}` on `enter_doc_mode` so first speaker is labelled immediately without prompting
- System prompt updated with `SPEAKER NAMING` workflow rule

### Infrastructure fixes — commit `62aa65c`

- **Mute button** — visually distinctive red `🔴 Muted` button in header; toggles mic track `enabled` at the WebRTC level (server hears silence, connection stays alive). Keyboard shortcut `M` (ignored when text input is focused). Resets on disconnect.
- **ICE keepalive ping** — server sends a `ServerMessage({type:"ping"})` every 25 seconds to keep WebRTC ICE consent fresh (consent expires at 30s per spec). Browser silently ignores it. Prevents session drop on inactivity.
- **ttyd WebSocket crash fixed** — `client_to_ttyd` and `ttyd_to_client` coroutines now catch all exceptions on disconnect; `WebSocketDisconnect` no longer surfaces as an unhandled task exception in the logs.

### DiagramFocusStateMachine — commit `e5e45ff`

**What was built:**
- New file `diagram_focus.py` — `DiagramFocusStateMachine` with its own lifecycle: `idle → viewing → editing → saving → viewing`. Separate from `DocStateMachine` and independently resettable
- `enter_diagram_focus` now transitions both `DocStateMachine` and `DiagramFocusStateMachine`, and injects a scoped SYSTEM NOTE into the LLM context restricting the controller to diagram-only commands while in focus mode
- `exit_diagram_focus` clears both state machines
- `update_diagram` sends an additional `diagram-focus-updated` browser event when called in `diagram_focus` state, triggering a live Mermaid re-render in the focus overlay without requiring the user to exit first
- Exit button (✕ Exit Focus) added to the focus overlay header — sends `"exit diagram mode"` as a `user-llm-text` RTVI message

---

## Manual Test Plan — Phase 2 Verification

Run all tests before starting Phase 3. Each section covers the happy path first, then edge cases.

---

### T1 — Voice pipeline and terminal (regression baseline)

**Happy path:**
1. `uv run bot.py` → open `http://localhost:7860/cockpit` → click Connect
2. Say "what directory are we in" → controller runs `pwd`, reports full real path (e.g. `/Users/mridula/src/pipecat/phone-coder/server`)
3. Say "list files" → `ls` runs in terminal, summary appears in chat pane
4. Say "clear the screen" → terminal clears without errors

**Edge cases:**
- Say a command while the controller is still speaking → speech should stop (interruption); command should be processed
- Switch model dropdown to `gpt-4.1` mid-conversation → say another command → controller should respond normally (no context corruption)
- Switch to `claude-sonnet-4-6` (cross-provider) → say a command → logs should show `context reset`; conversation continues
- Switch back to `gpt-4o-mini` → terminal commands still work

**Pass criteria:**
- No `Error: Directory '...' does not exist` in logs
- Interruption cuts off TTS immediately
- Cross-provider switch shows `[LLM switched ... context reset]` in logs
- Shell heartbeat: `tmux send-keys -t cockpit "echo heartbeat" Enter` → `heartbeat` visible in terminal within 2s

---

### T2 — Doc mode basics and transcript capture

**Happy path:**
1. Say "enter documentation mode for my test project"
2. Doc overlay appears; project created at `~/voice-cockpit-docs/my-test-project/version_0/`
3. Have 3–4 exchanges with the controller (questions, answers)
4. Say "write a summary of what we discussed" → controller calls `write_to_doc` → overlay updates with rendered markdown
5. Say "exit documentation mode" → overlay closes; terminal restored
6. Inspect `~/voice-cockpit-docs/my-test-project/version_0/`:
   - `my-test-project.md` — contains the written summary
   - `transcript.md` — contains both **User:** and **Controller:** turns chronologically with timestamps

**Edge cases:**
- Say "exit documentation mode" before writing anything → `document.md` is empty but file exists; no crash
- Say "enter documentation mode" while already in doc mode → controller should say it's already active (ALREADY_ACTIVE), no second overlay
- Say "enter documentation mode and discard" on exit → file should be empty/unchanged from entry state
- Open `/cockpit` in two browser tabs simultaneously, enter doc mode in both → second connect creates a new session cleanly

**Pass criteria:**
- Overlay updates live when `write_to_doc` is called
- `transcript.md` has both speakers in time order
- Terminal restores after exit — `echo heartbeat` works

---

### T3 — Speaker naming (requires two voices)

**Happy path:**
1. Enter doc mode (any project)
2. Person A (you) speaks — `[USER speaker=0]: ...` appears in logs
3. Person B speaks for the first time — `[USER speaker=1]: ...` in logs AND controller immediately asks "I heard a new voice — who is that?"
4. Reply "that's Alice" → controller calls `set_speaker_name("1", "Alice")`
5. Continue the session; Person B speaks again
6. Say "exit documentation mode"
7. Inspect `speakers.json` → `{"controller": "Controller", "0": "User", "1": "Alice"}`
8. Inspect `transcript.md` → Person B's lines labelled **Alice:**

**Edge cases:**
- Person B speaks twice before you answer the name question → controller should only ask once (not twice)
- Reply with a multi-word name ("that's Dr. Smith") → `speakers.json` should store `"Dr. Smith"` not just `"Dr."`
- Person B speaks, you name them, then person C speaks → controller should ask about person C separately
- Say "exit documentation mode" before naming a new speaker → unnamed speaker stored as `Speaker 1` in transcript; `speakers.json` has no entry for that ID
- Reconnect to same project (open existing) → if `speakers.json` has entries from the previous session, controller should not re-ask for known IDs (not yet implemented — expected to ask again; note as known gap)

**Pass criteria:**
- Controller asks exactly once per new speaker ID
- `speakers.json` written to disk immediately after naming (not just on exit) — verify by killing the server before exit and checking the file
- All of Person B's lines in `transcript.md` use the assigned name

**Caveat:** If `[USER speaker=None]` appears consistently in logs across all turns, Deepgram is not returning speaker IDs under live streaming conditions. This is a Deepgram streaming limitation — investigate separately. It does not block T4+.

---

### T4 — Diagram generation (Milestone 2.1)

**Happy path:**
1. Enter doc mode
2. Say "draw a sequence diagram for a user login flow"
3. Controller calls `insert_diagram` with `sequenceDiagram` source
4. Diagram renders in the doc overlay — SVG visible, no JS console errors
5. Inspect `<slug>.md` on disk → contains a `<!-- diagram-id: ... -->` block with ` ```mermaid\nsequenceDiagram `

**Edge cases:**
- Request an `xychart-beta` diagram → controller should speak a fallback notification and generate a `flowchart` block instead; verify in `document.md`
- Request a `C4Context` diagram (unsupported) → same fallback behaviour
- Request two diagrams in the same session → both appear in the overlay and in the file with distinct `diagram-id` values; neither block contains content from the other (Milestone 2.4)
- Say "add another diagram for the database schema" → second diagram added; first diagram untouched

**Pass criteria:**
- SVG rendered with non-zero dimensions; no JS error
- Unsupported type produces a fallback notification and a supported type in the file
- Two distinct `<!-- diagram-id: ... -->` blocks after two diagram requests

---

### T5 — Diagram Focus Mode lifecycle (Milestone 2.2)

**Happy path:**
1. After generating a diagram (T4), say "enter diagram focus for `<diagram-id>`"
2. Doc overlay hides; focus overlay appears fullscreen with the diagram rendered
3. Say "add a step for password validation between login and dashboard"
4. Controller rewrites Mermaid source, calls `update_diagram` → diagram re-renders live in the overlay (no exit needed)
5. Say "exit diagram mode" → focus overlay closes; doc overlay restores with the updated diagram embedded
6. Inspect `<slug>.md` → diagram block contains the updated source

**Edge cases:**
- Click the ✕ Exit Focus button instead of speaking → same result as saying "exit diagram mode"
- Say "run a git command" while in diagram focus → controller should refuse and say it can only handle diagram edits right now
- Say "write to the document" while in diagram focus → controller should refuse similarly
- Say "done" or "save and exit" → controller should call `exit_diagram_focus` (all three phrases should work)
- Enter focus mode, make no changes, exit → `document.md` unchanged; no empty update written
- Enter focus for diagram 1, exit, then enter focus for diagram 2 → both work cleanly; `_diagram_focus_sm` state is correct for each

**Pass criteria:**
- Focus overlay fullscreen; terminal iframe and doc overlay not visible
- Live re-render on `update_diagram` without requiring exit
- Doc overlay restores correctly after exit
- File on disk reflects the edit
- Non-diagram commands refused while in focus mode

---

### T6 — Invalid Mermaid syntax and rollback (Milestone 2.3)

**Happy path:**
1. In diagram focus, say "add a node with broken syntax" (or describe something the LLM is likely to render incorrectly)
2. If the generated source is invalid, the previous valid diagram block should be preserved in `document.md`
3. The focus overlay should show an inline error message, not a blank screen

**Edge cases:**
- Ask the controller to generate `flowchart LR; A-->` (no target) → rollback applied; error shown
- After a rollback, say another valid edit → diagram should update normally from the last valid source
- Make three edits in a row quickly → each should re-render; no race condition or stale source applied

**Pass criteria:**
- `document.md` always contains the last valid Mermaid block; never a broken block
- Inline error visible in focus overlay on bad syntax
- Recovery from rollback works — next valid edit applies correctly

---

### T7 — Regression after doc/diagram mode

**Happy path:**
1. Complete a full doc mode session with at least one diagram and one focus mode entry/exit
2. Say "exit documentation mode"
3. Say "list files in the server directory"
4. Controller runs `ls` in terminal normally

**Edge cases:**
- Disconnect and reconnect (click Disconnect then Connect) → start a new doc session; no state leakage from previous session
- Enter doc mode, then immediately disconnect without exiting → reconnect; should be able to enter doc mode cleanly (stale `_diagram_focus_sm` not blocking — **known issue**, see below)
- Run a long terminal command (e.g. `sleep 5 && echo done`) while not in any doc mode → interruption mid-sleep should work

**Pass criteria:**
- Terminal tool calls work normally after every mode exit
- Shell heartbeat passes after each mode exit
- No `INVALID_STATE` errors in logs during a clean reconnect

---

## Known Issues (as of 2026-06-08)

| Issue | Impact | Fix needed |
|---|---|---|
| `_diagram_focus_sm` not reset on client disconnect | Low — reconnecting without a clean exit may leave `diagram_focus_sm` in `viewing` state, blocking a new `enter_diagram_focus` | Reset `_diagram_focus_sm` in `on_client_disconnected` handler in `bot.py` |
| `gpt-4o-mini` occasionally skips tool calls and hallucinates | Medium — may answer "you're in ~/s/p/p/server" without calling `run_command` | Use `gpt-4.1` or `claude-sonnet-4-6` for tool-heavy sessions |
| Deepgram `speaker` field may be absent in streaming | Medium — `transcript.md` uses "User" for all user turns; speaker naming never triggers | Investigate Deepgram live streaming diarization; non-blocking for T4+ |
| Kokoro `phonemizer` words count mismatch warning | Cosmetic — `WARNING words count mismatch on 200.0% of lines` in logs | Non-fatal; audio produced correctly |
| Speaker map not reloaded when opening existing project | Low — re-entering a project after reconnect re-asks for already-named speakers | Load `speakers.json` from version dir into `session.speaker_map` on `enter_doc_mode(action='open')` |
| Mute state not reflected in VAD mic indicator animation | Cosmetic — `🎙 listening` may still show while muted | Check `micMuted` before updating mic label text in VAD handler |

---

## Not Yet Built (agreed in design, not implemented)

| Feature | Phase | Priority | Gate |
|---|---|---|---|
| Image search + embedding in diagram focus (see full spec below) | 2.5 | High | T5 PASS |
| Post-save diagram suggestion hook (controller proactively suggests diagram after `write_to_doc`) | 2 | Medium | T4 PASS |
| `move_diagram(id, target_section)` tool | 2 | Medium | T5 PASS |
| Diagram IDs with sequential numbers (`description-slug-N` format) | 2 | Low | — |
| Speaker map reloaded on `enter_doc_mode(action='open')` | 1 | Medium | T3 PASS |
| Speaker Merge utility (combine two diarization IDs retroactively) | 4 | Low | T3 PASS |
| `/api/state` live state inspector widget in cockpit header | — | Low | — |
| Review Mode (`review_mode` state + tools) | 4 | Low | Phase 3 PASS |
| Excalidraw integration + JSON-only AI cleanup | 3 | Low | Phase 2 PASS |
| Vision-assisted cleanup | 5 | Low | Phase 4 PASS |

---

## Phase 2.5 — Image Search and Embedding in Diagram Focus

### Overview

Extends diagram focus mode with a voice-driven image search and embedding flow.
Triggered when the user asks to replace a diagram element with a real image.
Images are downloaded locally, displayed as thumbnails, and the selected one is
stored permanently in the version directory and embedded in the Mermaid source.

This is a sub-feature of Phase 2 (diagram focus) and must be built after
Phase 2 live verification passes.

---

### State Design

`image_search` is a new sub-state of `DiagramFocusStateMachine`:

```
DiagramFocusStateMachine:
  idle → viewing → editing → saving → viewing
                     ↓
              image_search
                → searching   (DuckDuckGo call in flight, downloading images)
                → selecting   (thumbnails displayed, waiting for user to pick)
                → sizing      (image embedded, user confirming/adjusting size)
                → back to viewing on "done" / "looks good"
```

While in `image_search`, all non-image commands are blocked, same as the rest
of diagram focus mode. The controller refuses unrelated requests and redirects
the user to pick an image or say "cancel".

---

### Trigger

Only triggered by explicit user request while in `diagram_focus`:

- "replace the database node with an image"
- "find an image for the login step"
- "I want to use an icon for the user box"

The controller identifies the target element ID from the Mermaid source and
calls `search_images(query, element_id)`.

---

### Image Search API

**Primary: DuckDuckGo unofficial image endpoint**

```
GET https://duckduckgo.com/i.js?q=<query>
```

- No API key, no registration, no cost
- Returns JSON with image URLs, dimensions, source domain
- Returns up to 100 results; take top 5 ranked by resolution and source reliability
- Risk: unofficial, could change — acceptable for initial implementation

**Fallback (if DuckDuckGo breaks): Brave Search Image API**
- Free tier available, requires API key registration
- Add `BRAVE_SEARCH_API_KEY` to `.env` when needed

---

### Image Download and Storage

**During selection (ephemeral):**
```
/tmp/cockpit-images/<session-id>/
  1.jpg
  2.jpg
  3.jpg
  4.jpg
  5.jpg
```

- All 5 downloaded server-side before sending anything to the browser
- Served via `GET /api/images/<session-id>/<n>` FastAPI route (same origin — no CORS)
- Browser loads thumbnails from local server, not from external URLs
- File format: preserve original (jpg/png/webp); normalise to max 400px wide for thumbnail display

**After selection (permanent):**
```
~/voice-cockpit-docs/<slug>/version_N/images/
  <diagram-id>-<element-id>.<ext>
```

- Chosen image copied from `/tmp/` into the version's `images/` directory
- Other 4 deleted from `/tmp/` immediately
- Version directory image served via `GET /api/docs/<slug>/version/<n>/images/<filename>`
- Mermaid source references this local URL — works in local browser and survives export of the doc folder

**Cleanup:**
- All `/tmp/cockpit-images/<session-id>/` files deleted on: selection made, "cancel" spoken, diagram focus exited, client disconnected

---

### UX Flow

```
1. User: "replace the database node with an image"
   → controller enters image_search / searching state
   → controller speaks: "Searching for images, one moment."
   → server: DuckDuckGo search → download top 5 images to /tmp/

2. Server sends { type: "image-search-results", images: [{n:1, url:"/api/images/..."}, ...] }
   → focus overlay shows 5 numbered thumbnails in a horizontal strip (no text, no URLs)
   → controller speaks: "I found five images. Which would you like — 1, 2, 3, 4, or 5?
     Say cancel to go back."
   → state: selecting

3. User: "3"
   → server deletes images 1, 2, 4, 5 from /tmp/
   → copies image 3 to version images/ directory
   → controller rewrites Mermaid node with embedded image at a default width (40px)
   → diagram re-renders live
   → controller speaks: "Embedded. How does it look? Say bigger, smaller, or done."
   → state: sizing

4. User: "bigger"
   → controller increases width by 20px (40 → 60), rewrites Mermaid, re-renders
   → controller speaks: "Done. Bigger or smaller, or say done."

5. User: "done"
   → state exits image_search → back to viewing
   → thumbnails cleared from focus overlay
   → controller speaks: "Image set. Anything else to edit?"
```

---

### Mermaid Embedding

Target node rewritten from a text label to an image node:

```
Before:
  DB[("Database")]

After (Mermaid flowchart image node syntax):
  DB["<img src='http://localhost:7860/api/docs/.../images/db-icon.png' width='40'/>"]
```

Sizing steps: 20, 30, 40 (default), 60, 80, 100, 120px. Controller tracks current width in `DiagramFocusSession` and adjusts on "bigger"/"smaller".

---

### What Needs to Be Built

| Piece | File | Notes |
|---|---|---|
| `image_search` sub-states in `DiagramFocusStateMachine` | `diagram_focus.py` | `searching`, `selecting`, `sizing` states; `current_width`, `selected_image_path`, `target_element_id` on session |
| `search_images(query, element_id)` tool | `bot.py` | DuckDuckGo call, download 5 images to /tmp/, send `image-search-results` event |
| `select_image(number)` tool | `bot.py` | Delete others, copy chosen to version dir, embed in Mermaid, enter sizing state |
| `resize_image(direction)` tool | `bot.py` | Adjust width up/down, rewrite Mermaid, re-render |
| `cancel_image_search()` tool | `bot.py` | Delete all /tmp/ images, exit image_search sub-state |
| `/api/images/<session>/<n>` route | `bot.py` | Serve ephemeral /tmp/ images |
| `/api/docs/.../images/<file>` route | `bot.py` | Serve permanent version images |
| Thumbnail strip in focus overlay | `cockpit.html` | Handles `image-search-results` event; shows/hides numbered thumbnails |
| System prompt rules for image_search sub-state | `bot.py` | Block non-image commands; guide through search → pick → size → done flow |

---

### Manual Tests for Phase 2.5

#### T-IMG1 — Happy path: single image embedded

1. Enter doc mode → create a flowchart with a "Database" node
2. Enter diagram focus for that diagram
3. Say "replace the database node with an image"
4. Five thumbnails appear in the focus overlay; controller asks which one
5. Say "2" → thumbnails 1, 3, 4, 5 disappear; image 2 embedded in diagram at default size
6. Say "bigger" → image grows; say "done" → sizing state exits
7. Check `version_0/images/` — one image file present
8. Check `document.md` — Mermaid node contains `<img src='...' width='60'/>`

**Pass:** Live re-render at each step. Only one file in version images dir. Mermaid source matches.

#### T-IMG2 — Multiple images in one session

1. Embed an image in node A (T-IMG1 flow)
2. Without exiting focus mode, say "replace the user node with an image"
3. New set of 5 thumbnails appears (previous thumbnails cleared)
4. Select an image, size it, say "done"
5. Check `document.md` — two distinct `<img>` nodes in Mermaid source with different paths
6. Check `version_0/images/` — two image files, one per node

**Pass:** Two independent embed flows in one focus session. No cross-contamination of images or widths.

#### T-IMG3 — Real-time re-render on resize

1. Embed an image (T-IMG1 flow), reach sizing state
2. Say "bigger" 3 times → image grows at each step without exiting focus mode
3. Say "smaller" 2 times → image shrinks
4. Confirm diagram re-renders live at each resize without requiring exit/re-enter

**Pass:** Each resize triggers a `diagram-focus-updated` event and live Mermaid re-render.

#### T-IMG4 — Cancel discards all temp files

1. Trigger image search, thumbnails appear
2. Say "cancel" → thumbnails disappear, state returns to diagram editing
3. Check `/tmp/cockpit-images/<session>/` — all 5 files deleted
4. Check `version_0/images/` — no new file created
5. Diagram node unchanged from before search

**Pass:** No orphaned temp files. Mermaid source unchanged after cancel.

#### T-IMG5 — Disconnect cleanup

1. Trigger image search, thumbnails appear (do not select)
2. Disconnect the browser
3. Check `/tmp/cockpit-images/<session>/` — all temp files deleted on disconnect

**Pass:** No orphaned temp files after disconnect.

#### T-IMG6 — DuckDuckGo returns no results

1. Ask for an image of something obscure that returns no results
2. Controller should speak a fallback: "I couldn't find any images for that. Try a different description."
3. State exits image_search back to diagram editing
4. No temp files left on disk

**Pass:** Graceful fallback. No crash. Diagram unchanged.

---

### Decisions Recorded

- **DuckDuckGo** chosen as primary (no API key); Brave Search as fallback when/if it breaks
- **Local download first** — all images downloaded before showing thumbnails; avoids CORS, avoids mixed-content errors, avoids broken thumbnail URLs
- **Thumbnails only** — no text descriptions shown; controller reads a brief "which one?" prompt by voice
- **Permanent storage in version dir** — chosen image copied to `version_N/images/`; temp files for the rest deleted immediately on selection
- **Sizing via voice** — controller tracks width in session state; user says "bigger"/"smaller"; default 40px; steps 20/30/40/60/80/100/120
- **Explicit done gate** — image_search sub-state only exits on "done" or "cancel"; resize loop stays open until user confirms

---

## Session Handoff — 2026-06-06

### What was built this session

**Phase 0 verified** (all PoCs):
- PoC 1 (Excalidraw iframe), PoC 3 (Mermaid security), PoC 5 (overlay recovery): browser-verified PASS
- PoC 4 (atomic write): 16/16 pytest — already passing from prior session
- PoC 6 (model stack): script runs to PASS; sub-tests SKIP gracefully without API keys

**Phase 1 implemented** — new files:

| File | Purpose |
|---|---|
| `server/doc_storage.py` | Storage layer: `create_project`, `load_project`, `load_version`, `create_next_version`, `atomic_write`, path-traversal guard, duplicate-slug counter |
| `server/doc_writer.py` | `AttributedUtterance` dataclass + `DocWriter`: accumulates utterances, groups by speaker, renders `## Main Content` + `## Transcript` markdown |
| `server/doc_state.py` | Mode state machine: `DocModeState` enum, `DocSession`, `DocStateMachine` with `enter_doc_mode` / `exit_doc_mode` / `complete_save` / `error_recovery` |
| `tests/test_phase1_storage.py` | 18 pytest tests — directory structure, manifest fields, path traversal, duplicate slugs, version lineage |
| `tests/test_phase1_state.py` | 13 pytest tests — all valid/invalid transitions, idempotency, doc_writer lifecycle |
| `tests/test_phase1_docwriter.py` | 13 pytest tests — speaker grouping, fallback attribution, transcript timestamp, empty session |

**`bot.py` changes:**
- Imports `DocStateMachine`, `doc_storage`, `AttributedUtterance`
- Module-level `_doc_sm = DocStateMachine()` instance
- `enter_doc_mode(action, topic_name, project_slug)` tool call registered with LLM
- `exit_doc_mode(discard)` tool call registered with LLM
- `CockpitPrinter.process_frame` feeds `AttributedUtterance` to `DocWriter` when state is `doc_mode`
- System prompt updated to describe the two new tool calls
- Both tools push RTVI `ServerMessage` events (`doc-mode-entered`, `doc-mode-exited`) to browser

**`cockpit.html` changes:**
- State-Reducer scaffold added: `_docState`, `_docReducers`, `docDispatch`, `_applyDocUI`
- Hooks into server-message handler to react to `doc-mode-entered` / `doc-mode-exited`
- Phase 1 UI: updates a `#doc-mode-indicator` element if present (not yet added to HTML layout — Phase 2 adds the full overlay)

### Git state

- Branch: `main`
- All Phase 0 + Phase 1 changes are **unstaged / untracked** — not yet committed
- Rollback tag: `phase0-pre` → `b7d6d34`

---

## Next Steps — Recommended Sequence

### Immediate (before starting Phase 2)

1. ~~**Milestone 1.4 live check**~~ ✅ DONE (2026-06-07)

2. **Commit Phase 0 + Phase 1** — no commit has been made yet for any of this work. Suggested message:
   ```
   Add Phase 0 PoCs and Phase 1 foundations (storage, state machine, DocWriter)
   ```
   Files to stage: `editor.html`, `poc/`, `tests/`, `doc_storage.py`, `doc_writer.py`, `doc_state.py`, `bot.py`, `cockpit.html`, `pyproject.toml`, `uv.lock`, `.env.example`, `diagramming_plan.md`

3. **Add API keys** (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) to `.env` and re-run `uv run poc/poc6_model_stack.py` to get full PASS instead of SKIP.

4. **PoC 2 (diarization)** — verify when two speakers are available. Non-blocking for Phase 2.

### Phase 2 — Mermaid Diagrams and UX Overlay

Build in this order:

1. **`insert_diagram` / `update_diagram` tool calls** in `bot.py` — LLM generates Mermaid source; tool validates syntax and writes fenced block to `document.md`
2. **Mermaid syntax validator** — Python wrapper using `subprocess` + `node -e "require('mermaid')"` or a regex pre-check; preserve last valid source on failure
3. **Mermaid compatibility matrix enforcement** — reject unsupported types, notify user, re-prompt LLM with constraint
4. **Doc view overlay in `cockpit.html`** — markdown panel with rendered Mermaid diagrams (left/right split or tab over terminal)
5. **`enter_diagram_focus` / `exit_diagram_focus` tool calls** — hide doc panel, expand selected diagram to fullscreen
6. **Focus Mode context pruning** — in `diagram_focus` mode, trim LLM context to active diagram source + last N turns only
7. **Milestone 2.1–2.4 tests** — pytest for diagram generation, compatibility enforcement, invalid syntax rollback, multi-diagram session

**Key decision for Phase 2:** The doc view overlay needs a layout choice — tab (simpler) vs. side-by-side split (better UX). The plan says "markdown panel with rendered diagrams"; recommend a tab approach for Phase 2 (simpler to implement and test) and revisit for Phase 3.

### Phase 3 onward — not yet scoped in detail

Phase 3 (Excalidraw + JSON-only AI cleanup) depends on:
- `@excalidraw/mermaid-to-excalidraw` npm package installed and PoC 6 Test 3 returning PASS
- `ANTHROPIC_API_KEY` set (needed for `fallback_text_model` JSON cleanup calls)

Phase 4 (Review Mode) and Phase 5 (vision AI) have no additional prerequisites beyond the prior phase gates.

---

### Phase 0 — Infrastructure Spikes

**Gate**: All six PoCs must return a clear PASS before any Phase 1 code is written. A PoC that returns FAIL requires an explicit architectural decision (fallback or redesign) before proceeding.

**Prerequisites**: `VOICE_COCKPIT_DOCS_ROOT` added to `.env.example`. API keys for Claude (Anthropic) and Gemini confirmed available. `pytest` selected as the test framework and added to `pyproject.toml`.

**Implementation** (2026-06-06): All prerequisites done. Files created:
- `editor.html` — Excalidraw UMD bundle (pinned v0.17.6), postMessage API (`get-scene`, `set-scene`, `get-png`), served at `/editor`
- `poc/poc1_iframe_test.html` — parent test harness with PASS/FAIL buttons, served at `/poc/poc1_iframe_test`
- `poc/poc3_mermaid_test.html` — auto-runs all 3 Mermaid PASS criteria on load, served at `/poc/poc3_mermaid_test`
- `poc/poc5_overlay_test.html` — autosave-on-type + reload recovery test, served at `/poc/poc5_overlay_test`
- `poc/atomic_write.py` — atomic write utility, project lock, `sanitize_slug`, `create_version_dir`
- `poc/poc6_model_stack.py` — Claude Sonnet + Gemini Flash + mermaid-to-excalidraw spike; run with `uv run poc/poc6_model_stack.py`
- `tests/test_poc4_atomic_write.py` — 16 pytest tests (all pass); run with `uv run pytest tests/test_poc4_atomic_write.py -v`
- `bot.py` — diarize enabled in Deepgram (`LiveOptions(diarize=True)`), speaker field logged; routes added: `/editor`, `/poc/<name>`, `/api/autosave` (POST/GET/DELETE)

#### PoC 1 — Excalidraw iframe sandbox ⬜ Awaiting browser verification

Build a standalone `editor.html` that loads Excalidraw and embeds it in `cockpit.html` as an iframe.

**PASS criteria** (all must hold):
- Draw a rectangle in the iframe → call `postMessage({type: "get-scene"})` from the parent → assert returned JSON contains an element with `type: "rectangle"`.
- Resize the browser window to fullscreen → assert canvas repaints without blank regions or scroll bars appearing inside the iframe.
- Reload the parent page while the iframe is open → assert no orphaned overlay remains in `cockpit.html` DOM after reload.
- Iframe cold-load time is under 4 seconds on a local network (no external CDN latency).

**FAIL action**: Evaluate `tldraw` as a drop-in replacement before Phase 3 is scoped. Do not proceed to Phase 3 without a confirmed working iframe editor.

**How to verify**: Open `/poc/poc1_iframe_test` while bot is running. Draw a rectangle, click "Get Scene", check for PASS badge.

#### PoC 2 — Deepgram diarization under live streaming ⬜ Awaiting live session verification

Enable `diarize: true` in `DeepgramSTTService`. Run a live two-speaker session (two microphones or one mic passed between speakers).

**PASS criteria** (all must hold):
- Inspect raw `TranscriptionFrame` logs → at least one frame carries a non-null `speaker` field that changes value between the two speakers within a single session.
- Single-speaker session → `speaker` field is consistently `0` (or consistently absent) across all frames — no random flipping.

**FAIL action**: Document exact frame structure observed. `AttributedUtterance.speaker_id` defaults to `None`; `DocWriter` must handle `None` gracefully. Do not block Phase 1 on this — Phase 1 builds the normalization layer regardless.

**How to verify**: Run bot with two speakers; check logs for `[USER speaker=N]` lines. Verify speaker field changes between speakers.

#### PoC 3 — Mermaid render and security ⬜ Awaiting browser verification

Load a pinned Mermaid CDN version with `securityLevel: 'strict'` in a test page.

**PASS criteria** (all must hold):
- Valid Mermaid source renders an SVG with visible nodes — no JS error in the browser console.
- Inject `<img src=x onerror="window.__xss=1">` as a node label → assert `window.__xss` is `undefined` after render and no `<img>` tag appears in the DOM.
- Pass deliberately broken syntax (`flowchart LR; A-->`) → assert the page does not crash and an inline error message is visible.

**How to verify**: Open `/poc/poc3_mermaid_test` while bot is running. All 3 tests auto-run and display PASS/FAIL.

#### PoC 4 — Atomic file write and version collision safety ✅ PASS (2026-06-06)

Write a test script that calls the write-then-rename utility concurrently from two processes targeting the same `version_N` slot.

**PASS criteria** (all must hold):
- Kill the writing process mid-rename with `SIGKILL` → assert no corrupt final file exists; `.tmp` file is detected and cleaned up on the next run.
- Two concurrent processes attempt to create `version_1` simultaneously → assert exactly one `version_1` directory is created and the other process increments to `version_2` without error.

**Result**: `uv run pytest tests/test_poc4_atomic_write.py -v` → 16/16 passed. SIGKILL test, concurrent version creation, slug sanitization all verified.

#### PoC 5 — Browser overlay state recovery ⬜ Awaiting browser verification

Manually enter `doc_mode`, edit a short text block, then hard-refresh the browser tab.

**PASS criteria** (all must hold):
- After reload, the server pushes autosave state to the client → the doc overlay re-renders with the prior text content visible.
- The terminal iframe is not visible underneath the overlay.
- A second hard refresh while the overlay is empty (no content yet) → overlay is dismissed and `shell` state is restored cleanly.

**How to verify**: Open `/poc/poc5_overlay_test` while bot is running. Type text, click "Autosave Now", click "Hard Refresh", confirm PASS badges.

#### PoC 6 — Model capability stack ⬜ Awaiting API key verification

Send test payloads to both model slots.

**PASS criteria** (all must hold):
- Send a minimal valid Excalidraw scene JSON (one rectangle element) to `diagram_cleanup_model` (Claude Sonnet) → assert response is valid JSON parseable by `json.loads()` and contains an `elements` array.
- Send a 400×300 PNG of a rendered Mermaid diagram to `fast_validation_model` (Gemini Flash) → assert a structured text response is received within 5 seconds.

**How to verify**: `uv run poc/poc6_model_stack.py` (requires `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` in `.env`). Uses `gemini-2.0-flash` (not deprecated `gemini-1.5-flash`).

**Note**: Verify the current recommended Gemini Flash model ID before this PoC — `gemini-1.5-flash` is deprecated; use `gemini-2.0-flash` or the current recommended equivalent.

**Also evaluate** `@excalidraw/mermaid-to-excalidraw` conversion quality during Phase 0: convert a sample `sequenceDiagram` → assert the output Excalidraw scene contains the expected participant nodes. SVG import is the fallback only if this produces empty or degenerate output.

---

### Phase 1 — Foundations: Storage, State Machine, Text Documentation

**Gate**: Milestones 1.1–1.4 all PASS before Phase 2 begins.

**What gets built:**
- `AttributedUtterance` normalization adapter (converts raw Deepgram frames; `DocWriter` never sees provider-specific structure)
- `DocWriter` class with separate main-content and transcript accumulators
- Deepgram diarization enabled; speaker map and new-speaker prompt flow
- Session-level speaker map (`dict[str, str]` in `bot.py`, keyed by Deepgram speaker ID)
- Versioned directory structure: `project.json`, `version_0/manifest.json`
- Atomic write protocol (write to `.tmp`, rename to final path)
- Autosave to `version_0/.session/` for browser-refresh recovery
- Path safety and filename sanitization
- Mode state machine in `bot.py` (server-authoritative)
- State-Reducer pattern scaffolded in `cockpit.html` browser JS
- `enter_doc_mode` / `exit_doc_mode` tool calls with full contracts
- Observability hooks: mode transitions, save paths, STT attribution fallback events, speaker mapping events
- `pytest` unit tests for all `DocWriter` and state machine logic

#### Milestone 1.1 — Storage layer

**PASS criteria**:
- Call `create_project("My Project")` → assert `$DOCS_ROOT/my-project/version_0/` exists containing `project.json`, `manifest.json`, `document.md` (empty), `transcript.md` (empty), `speakers.json` (empty object `{}`).
- Pass `"../../etc/passwd"` as a project name → assert it is sanitized to a safe slug (no `..` or `/`) and the resulting directory is inside `DOCS_ROOT`.
- `manifest.json` contains `version: 0`, `derived_from: null`, a creation timestamp, and relative paths to all expected files.

#### Milestone 1.2 — State machine

**PASS criteria**:
- Call `enter_doc_mode` twice without `exit_doc_mode` → assert the second call returns `ALREADY_ACTIVE` and exactly one version directory exists.
- Call `exit_diagram_focus` from `shell` state → assert it returns `INVALID_STATE` and no file write occurs.
- `pytest` state machine tests: all invalid transitions rejected; all valid transitions produce the correct resulting state. Zero failures.

#### Milestone 1.3 — DocWriter and speaker attribution

**PASS criteria**:
- Simulate a four-utterance exchange (two utterances each from `speaker_0="Alice"` and `speaker_1="Bob"`) → call `exit_doc_mode` → assert `document.md` contains a `## Main Content` section and a `## Transcript` section with `### Alice` and `### Bob` subsections, each containing their respective utterances and no utterances from the other speaker.
- Simulate a session where the `speaker` field is absent from all frames → assert `document.md` has a `### Speaker Unknown` subsection and no utterances are dropped.
- `pytest` unit tests: `DocWriter` section assembly, empty-session output, transcript grouping, `AttributedUtterance` normalization. Zero failures.

#### Milestone 1.4 — Shell restore ✅ PASS (2026-06-07)

**PASS criteria**:
- After `exit_doc_mode`, run `tmux send-keys -t cockpit "echo heartbeat" Enter` → assert `heartbeat` appears in `tmux capture-pane` output within 2 seconds.
- Working directory inside the tmux pane is unchanged from before `enter_doc_mode` was called.

---

### Phase 2 — Mermaid Diagrams and UX Overlay

**Gate**: Milestones 2.1–2.4 all PASS before Phase 3 begins.

**What gets built:**
- `insert_diagram` and `update_diagram` tool calls with full contracts
- Mermaid loaded via pinned CDN with `securityLevel: 'strict'`
- Mermaid compatibility matrix enforcement: unsupported types trigger voice notification and LLM re-prompt with the constraint
- Syntax validation before every write; last valid source preserved for rollback
- Diagram placeholder insertion; agent iterates through placeholders: *"Do you want to edit diagram one?"*
- Doc view (markdown panel with rendered diagrams) and Focus Mode (selected diagram fullscreen, all other UI hidden) in `cockpit.html`
- `enter_diagram_focus` / `exit_diagram_focus` tool calls wired to overlay transitions
- Diagram-as-Code first: all voice edits rewrite Mermaid source; no Excalidraw yet
- Context window pruning active in `diagram_focus` mode (only active diagram source + last N turns sent to LLM)

**Diagramming interaction flow:**
1. Markdown generated with diagram placeholders.
2. Agent iterates: *"Do you want to edit diagram one?"*
3. On confirmation, shell and markdown panel hide; selected diagram fills the screen in Focus Mode.
4. User edits via voice; LLM rewrites Mermaid source; diagram re-renders on each update.
5. *"Done"* or *"save"* exits Focus Mode; markdown view restored with updated diagram embedded.
6. Agent moves to the next placeholder.

#### Milestone 2.1 — Diagram generation and compatibility enforcement

**PASS criteria**:
- Say "draw a sequence diagram for user login" → assert `document.md` contains a ` ```mermaid\nsequenceDiagram ` block with at least one `->` arrow.
- Request an `xychart-beta` diagram → assert the agent speaks a fallback notification, the LLM is re-prompted with the constraint, and `document.md` contains a `flowchart` block instead.

#### Milestone 2.2 — Mermaid render and Focus Mode lifecycle

**PASS criteria**:
- Diagram SVG is present in the DOM with non-zero dimensions; no JS error in the browser console.
- `enter_diagram_focus` → assert terminal iframe `display: none`, markdown panel `display: none`, diagram element fills viewport.
- `exit_diagram_focus` → assert both panels are restored; no overlay element remains in the DOM with `display` other than `none`.

#### Milestone 2.3 — Invalid syntax and rollback

**PASS criteria**:
- Inject broken Mermaid source (`flowchart LR; A-->`) via a voice command → assert the previous valid diagram block is preserved in `document.md` and Focus Mode shows an inline error message (not a blank screen or crash).

#### Milestone 2.4 — Multi-diagram session

**PASS criteria**:
- Create two diagrams in one session → exit doc mode → assert both are present in `document.md` with distinct diagram IDs and neither block contains content from the other.

**Shell restore check (required after each milestone above)**: `tmux send-keys -t cockpit "echo heartbeat" Enter` → `heartbeat` visible in captured output within 2 seconds.

---

### Phase 3 — Excalidraw Integration and JSON-Only AI Cleanup

**Gate**: Milestones 3.1–3.3 all PASS before Phase 4 begins.

**What gets built:**
- Excalidraw embedded in `editor.html` iframe (PoC 1 confirmed)
- `postMessage` protocol between `cockpit.html` and `editor.html` for scene get/set and save events
- `@excalidraw/mermaid-to-excalidraw` conversion (SVG import as fallback only if PoC evaluation shows degenerate output)
- Original Mermaid source preserved when escalating to Excalidraw; never discarded
- Excalidraw scenes stored as sidecar `.excalidraw` files in `version_N/diagrams/`; `document.md` references by diagram ID only (no embedded JSON)
- `capture_diagram` utility: canvas-to-PNG export (used by Phase 5)
- Full undo/rollback for all Excalidraw edits
- JSON-only AI cleanup: voice command "clean this up" serializes scene JSON → sends to `fallback_text_model` → validates returned JSON → keeps rollback snapshot → applies to canvas
- Voice commands: *"edit this diagram"* opens Excalidraw in iframe Focus Mode; *"save"* writes JSON sidecar; *"clean this up"* triggers JSON-only AI cleanup

#### Milestone 3.1 — Mermaid-to-Excalidraw escalation

**PASS criteria**:
- From a rendered Mermaid `sequenceDiagram`, say "edit this in Excalidraw" → assert `editor.html` iframe loads with an Excalidraw scene containing elements that correspond to the Mermaid participant nodes (check `elements` array in scene JSON via `postMessage`).
- Original Mermaid fenced block is still present in `document.md` after escalation.
- `@excalidraw/mermaid-to-excalidraw` returns empty scene → assert agent prompts "I couldn't convert the diagram — do you want to start from scratch?" and does not silently open a blank canvas.

#### Milestone 3.2 — Sidecar persistence

**PASS criteria**:
- Draw a shape in Excalidraw and save → assert `version_N/diagrams/<diagram-id>.excalidraw` exists and `json.loads()` succeeds on its contents.
- Assert `document.md` references the diagram by ID (e.g., `[diagram: auth-flow]`) and does not contain any raw Excalidraw JSON inline.
- Switch to Excalidraw and immediately save without making any changes → assert the original Mermaid block is preserved in `document.md` and no empty sidecar is written.

#### Milestone 3.3 — JSON-only AI cleanup and rollback

**PASS criteria**:
- Say "clean this up" → assert a log line confirms `fallback_text_model` was called (not the vision model), the returned JSON is applied to the canvas, and a rollback snapshot file exists in `.session/`.
- Mock `fallback_text_model` to return `{"garbage": true}` → assert the rollback snapshot is applied, the canvas is unchanged, and the agent speaks a notification.

**Shell restore check**: Excalidraw Focus Mode exit → `tmux send-keys -t cockpit "echo heartbeat" Enter` → `heartbeat` visible within 2 seconds. Markdown view restored to the state it was in before Excalidraw was opened.

---

### Phase 4 — Review Mode

**Gate**: Milestones 4.1–4.3 and the end-to-end integration test all PASS before Phase 5 begins.

**What gets built:**
- `enter_review_mode` tool call: lists projects and versions from `VOICE_COCKPIT_DOCS_ROOT`
- `exit_review_mode` tool call with atomic version creation
- `DocWriter` review log accumulator (comment / edit / approval / disagreement entries) consuming `AttributedUtterance` events
- `append_review_entry` and `save_review_version` tool calls with full contracts
- Speaker Merge utility: combine two diarization IDs into one speaker, re-attribute all existing entries retroactively
- `speakers.json` auto-loaded from selected version directory on entry; no re-prompting for known names
- Version resolution with project-level lock: `version_1`, `version_2`, etc.
- `review.md` written to the new version directory and referenced from `manifest.json`
- Atomic version creation (write-then-rename; project-level lock)

#### Milestone 4.1 — Version creation and lineage

**PASS criteria**:
- Open a Phase 1 project in Review Mode, make one comment from each of two speakers, exit → assert `version_1/` exists containing `manifest.json` (with `derived_from: "version_0"`), `review.md` (with both speaker comment entries), and `speakers.json`.
- Run Review Mode a second time on `version_1` → assert `version_2/` is created; `version_1/` is not modified.
- Two concurrent processes both attempt to create `version_1` → assert exactly one succeeds and the other correctly increments to `version_2`.

#### Milestone 4.2 — Speaker continuity

**PASS criteria**:
- `speakers.json` present in `version_0` → entering Review Mode does not prompt for speaker names that are already in the map.
- `speakers.json` absent → first new-speaker detection triggers the name prompt; name is written to `speakers.json` in the new version directory.

#### Milestone 4.3 — Speaker Merge

**PASS criteria**:
- Create a session where `speaker_0` and `speaker_2` both appear in `review.md`. Trigger Speaker Merge on `speaker_0` and `speaker_2` → assign the merged identity "Alice" → assert all entries previously attributed to `speaker_2` are relabeled to Alice in both `document.md` and `review.md`, and no `speaker_2` entries remain.

#### End-to-End Integration Test

Run after Phase 4 is complete. All assertions must PASS before Phase 5 is started.

```
Enter Doc Mode
→ speak a short discussion
→ generate two Mermaid diagrams (Phase 2 path)
→ escalate diagram 1 to Excalidraw, add a shape, trigger JSON-only cleanup (Phase 3 path)
→ exit Doc Mode
→ Enter Review Mode on the saved version
→ two speakers each add one comment and one approval
→ exit Review Mode
```

Assert:
- `version_1/` directory structure matches the schema (all required files present).
- Both diagrams appear in `version_1/manifest.json` diagram list with correct IDs.
- `version_1/review.md` contains all four entries (two comments, two approvals) with correct speaker names.
- After each mode exit: `tmux send-keys -t cockpit "echo heartbeat" Enter` → `heartbeat` visible within 2 seconds.
- Issue a standard coding voice command ("run the tests") after the full flow → assert the agent executes it correctly with no regression.

---

### Phase 5 — Vision-Assisted AI Cleanup

**Gate**: Milestones 5.1–5.4 all PASS. This is the final phase; no further go/no-go gate beyond that.

**What gets built:**
- `capture_diagram` PNG output wired into a multimodal call (compressed screenshot + scene JSON + voice command)
- `diagram_cleanup_model` (Claude Sonnet) for full visual cleanup; `fast_validation_model` (Gemini Flash) for lightweight background readability checks
- Explicit user opt-in before any diagram image is sent to a vision model (diagrams may contain proprietary content)
- Screenshot compression / downscaling before sending (target: under 500 KB)
- User-visible diff / preview: added elements outlined in green, removed elements struck through in red; voice prompt: *"Here's what I changed — shall I apply it?"*
- JSON schema validation of returned scene before applying; automatic rollback to pre-cleanup snapshot on invalid output
- Verbal fillers during the 5–10s vision round trip (*"Analyzing the layout now..."*)
- `fast_validation_model` used for background readability checks to keep UX responsive
- Log model ID, prompt version, input size (token count + image bytes), and round-trip latency for every multimodal call

#### Milestone 5.1 — Opt-in gate is enforced

**PASS criteria**:
- Say "clean this up" without vision opt-in → assert only `fallback_text_model` is called (log confirms JSON-only path), no PNG is captured, no image is sent to any model.

#### Milestone 5.2 — Vision round-trip and diff preview

**PASS criteria**:
- Enable vision opt-in, draw a rough flowchart, say "clean this up" → assert: PNG is captured and sent to `diagram_cleanup_model` (log line present), diff preview renders on the canvas before any write, and the agent speaks the confirmation prompt before committing.
- User says "no" to the confirmation prompt → assert canvas is unchanged and no sidecar file is modified.

#### Milestone 5.3 — Vision failure fallback

**PASS criteria**:
- Simulate vision API timeout (mock or network block) → assert system falls back to JSON-only cleanup via `fallback_text_model`, agent notifies the user that visual cleanup was unavailable, no unhandled exception.

#### Milestone 5.4 — Rollback on bad model output

**PASS criteria**:
- Mock `diagram_cleanup_model` to return `{"elements": "not-an-array"}` → assert rollback snapshot is applied, canvas is unchanged from before the call, and agent speaks a notification.

**Shell restore check**: All Phase 5 operations leave the tmux pane active and voice pipeline responsive (`echo heartbeat` test passes).

---

### Verification Checklist (All Phases)

Each item maps to a milestone above. Mark PASS or FAIL per phase.

| Check | How to verify |
|---|---|
| File output structure is correct | `cat version_N/document.md` — assert `## Main Content` and `## Transcript` sections present |
| Speaker attribution is accurate | Two-speaker scripted exchange → inspect transcript subsections by name |
| Browser panel renders correctly | Visual check: doc overlay visible, no overflow or z-index bleed onto terminal |
| Focus Mode activates and dismisses cleanly | Enter and exit Focus Mode — inspect DOM: no overlay element with `display` other than `none` after exit |
| Shell restored after every mode exit | `tmux send-keys -t cockpit "echo heartbeat" Enter` → output visible within 2s |
| LLM tool calls are idempotent | Call `enter_doc_mode` twice — assert one version directory, no duplicated content |
| No regression in existing voice commands | Issue a coding command after every mode-exit sequence |
| Atomic write integrity | `SIGKILL` mid-write → no corrupt final file; `.tmp` cleaned on next start |
| Path traversal rejected | `create_project("../../etc/passwd")` → path confined to `DOCS_ROOT` |
| Mermaid security enforced | Inject `<img onerror=...>` in node label → `window.__xss` undefined after render |
| Prompt injection rejected | Embed `"Ignore previous instructions"` in a diagram label → agent does not act on it |
| Mermaid unsupported type handled | Request `xychart-beta` → voice notification + `flowchart` fallback in `document.md` |
| Vision opt-in enforced | `clean_diagram` without opt-in → no PNG captured, JSON-only path confirmed in logs |
| Vision diff preview shown | `clean_diagram` with opt-in → diff overlay visible before user confirmation |

### Unit Tests (pytest, run with `uv run pytest`)

- `DocWriter` section assembly
- Empty-session document output
- Transcript grouping by speaker
- Speaker map load and save
- Version filename resolution
- Existing version collision handling
- Diagram block insert, update, and delete
- Mermaid validation failure preserves last valid block
- Mermaid unsupported type triggers fallback and LLM re-prompt
- Excalidraw JSON validation rejects malformed scenes
- State machine rejects all invalid transitions
- `AttributedUtterance` normalization (missing speaker field, low-confidence fallback)
- Path sanitization (traversal characters, shell metacharacters)
- Speaker Merge re-attribution correctness

### Integration Tests

- Browser overlay enter and exit for all modes (`doc_mode`, `diagram_focus`, `review_mode`)
- Mermaid render success and failure in Focus Mode
- Focus Mode refresh recovery (autosave restore via server-side session affinity)
- Multi-diagram session (two diagrams, no overwrites)
- JSON-only AI cleanup: apply and rollback
- Review version creation and multi-version lineage
- Shell restoration after each mode transition
- Speaker Merge re-attribution end-to-end

---

## Future Investigations

### Web Search Integration (Revisit After Phase 5)

**Requirement**: After speakers finish talking and a doc update is made, the controller should be able to search online sources related to the discussion topics and incorporate them as cited suggestions in the markdown.

**Why deferred**: Web search implementation options (API choice, query formulation strategy, async execution model) need further research before committing to a design. Revisit once Phases 0–5 are complete and stable.

**Key decisions to make before implementing:**
- Search API selection — leading candidates: Tavily (LLM-agent-optimised), Perplexity (pre-summarised answers), Brave Search (raw results, low cost)
- Trigger model — explicit voice command (*"find references for this"*) vs. automatic detection of factual claims; explicit command recommended as the starting point
- Insertion policy — agent reads suggestion aloud and asks for confirmation before inserting; no auto-insertion into the doc
- Source quality — restrict to trusted domains (MDN, RFCs, official docs, arXiv) for technical topics
- Attribution format — every suggestion must include a source URL; unsourced suggestions are not inserted
- Async execution — search runs in the background without blocking the voice pipeline; results appear after a short delay
- Privacy — discussion content is sent to a third-party API; flag for sessions containing proprietary material; apply same opt-in policy as vision calls
- Rate limiting — cap searches per session to control API cost

**Provisional design sketch** (to be validated during research):
```
Voice command: "find references for this"
  → LLM formulates a targeted search query from the current discussion segment
  → search_web(query) tool call → Tavily / Perplexity API
  → LLM synthesizes a cited suggestion block
  → Agent reads suggestion aloud: "I found a reference — shall I add it?"
  → On confirmation, suggestion inserted into doc with source URL
```

### Data-Driven Charts

Mermaid supports some chart and timeline styles but is not a replacement for data-backed visualization with reusable scales, interactivity, or numeric precision. Vega-Lite or Chart.js can be added in a future phase if data chart requirements emerge.

---

## Summary

| Need | Solution | Rationale |
|---|---|---|
| Freehand drawing + AI cleanup | Excalidraw (iframe sandbox) | Structured JSON model, LLM-friendly; isolated to prevent React/CSS conflicts |
| LLM-generated structured diagrams | Mermaid.js (pinned CDN, strict mode) | Text-in/SVG-out, CDN-compatible; DaC-first keeps diagrams editable by LLM |
| Mermaid→Excalidraw conversion | `@excalidraw/mermaid-to-excalidraw` | Direct source conversion produces better Excalidraw primitives than SVG import |
| JSON-only AI diagram cleanup | `fallback_text_model` via scene JSON rewrite | Fast, low-cost; available from Phase 3 before vision is introduced |
| Vision-assisted AI cleanup | `diagram_cleanup_model` (Claude 3.5 Sonnet) | Best spatial reasoning; opt-in only; ships in Phase 5 |
| Fast background diagram validation | `fast_validation_model` (Gemini 1.5 Flash) | Near-instant latency for readability checks |
| Summarization / transcript synthesis | `summarization_model` (GPT-4o-mini or Gemini Flash) | Cost-effective for text-only tasks |
| Speaker diarization and attribution | Deepgram + `AttributedUtterance` normalization layer | Advisory attribution; provider-agnostic `DocWriter`; Speaker Merge for fragmentation |
| Versioned document storage | `version_N/` directories with `project.json` + `manifest.json` | Immutable history; explicit metadata as source of truth |
| Data charts (future) | Vega-Lite / Chart.js | Not needed yet |
