# Voice-Driven Documentation & Diagramming Plan

## Overview

The Voice Coding Cockpit's Documentation Mode lets a user build and edit documents
and diagrams by **talking and sketching**. This plan describes the architecture for
capturing that multimodal intent and turning it into clean, structured artifacts.

The core idea: capture user intent through a **synchronized event stream** —

- voice commands (timestamped)
- freehand annotations (circles, arrows, highlights, scribbles)
- the current artifact state (Markdown / Mermaid / Excalidraw JSON)
- rendered-output snapshots (SVG/PNG)
- event history and diffs (git)

— and have an **LLM/VLM interpret that stream and emit structured patches** to the
underlying artifact. The freehand layer is *disposable input*; the structured source
is what persists.

---

## Status (what already exists)

**Implemented (Phases 0–1):**
- Voice-driven Markdown editing — `write_to_doc`, `edit_doc`, `read_doc`, section-aware edits.
- Chronological transcript + `DocWriter`.
- **Git-backed storage** (single repo at `VOICE_COCKPIT_GIT_ROOT`; each project a folder;
  every save is a commit, auto-pushed to `origin`; push failures warn but never block).
- **Mermaid diagrams** — `insert_diagram`, `update_diagram`, `move_diagram`, diagram focus
  mode rendering Mermaid → SVG, image embedding in nodes.
- Mode state machine (`doc_state.py`): `shell` ↔ `doc_mode` ↔ `diagram_focus`.

**Rolled back / not present:**
- **Speaker diarization** was removed. Doc mode assumes a single user plus the controller
  (`speaker_map = {user, controller}`); transcripts are not multi-speaker attributed.
- No Excalidraw yet — diagram mode is Mermaid-only today.

**This plan covers** the new multimodal-intent layer (freehand + voice → patches) and the
introduction of Excalidraw as a *freehand* diagram engine alongside Mermaid.

---

## Core Architecture — Three Layers

The system separates three concerns that evolve independently:

### 1. Source of truth (structured, persisted)
The editable artifact, always in a **structured, diffable** format:
- **Markdown** for documents
- **Mermaid** (text) for generated/structured diagrams
- **Excalidraw JSON** for freehand diagrams (only once promoted — see lifecycle below)

SVG/PNG are *renderings* of the truth, never the truth — they lose semantic structure
and can always be regenerated. Git stores the history of these structured files.

### 2. Intent layer (disposable, screen-space)
A **transparent freehand-capture overlay** mounted on top of whatever is rendered
(Markdown preview, Mermaid SVG, or an Excalidraw canvas). It captures circles, arrows,
highlights, and scribbles — and is **thrown away** after interpretation. It never
becomes the source of truth.

Built **once** as a reusable component and reused over every substrate. (This is the
reason it's a separate overlay rather than drawing into the diagram canvas directly:
one capture surface that works identically over Markdown and diagrams.)

### 3. AI interpretation (event stream → patch)
Consumes the event stream (voice + annotations + current state + snapshot) and emits a
**structured patch** to the source of truth. Never edits SVG; always edits the structured
format, then re-renders.

### The interaction loop
```
voice + freehand overlay + current artifact state
  → event stream / timeline
  → LLM / VLM interpretation
  → structured intent
  → patch generation
  → update Markdown / diagram source
  → re-render clean output
  → clear temporary overlay
```

---

## The Freehand Overlay (intent layer detail)

### Screen-space capture, not shared cameras
The overlay captures strokes in **screen-pixel coordinates**. Correlation to the
substrate happens **at the moment a gesture completes** by hit-testing the stroke's
screen position against the substrate's *current* screen-space element bounds.

This deliberately avoids any persistent "two synced canvases / shared camera" machinery.
There is no camera to drift: you take a one-time screen-space projection at interpretation
time, resolve the target, then discard the strokes.

### The resolver contract (substrate-agnostic)
```
stroke geometry  → verb     (circle/lasso, arrow, underline/highlight, scribble-out)
hit-test target  → operand  (an addressable id in the source of truth)
timestamped voice → disambiguation
                  → structured patch
```

Gesture vocabulary:
- **circle / lasso** around a thing → "select this" (target for the next voice command)
- **arrow A → B** → "move / relate A to B" (this is how *"move this section here"* works)
- **underline / highlight** → "edit this span"
- **scribble-out** → "delete this"

The stroke gives the verb; the hit-tested target(s) give the operands; the voice
disambiguates. Build this contract once; point it at three substrates.

### Per-substrate adapter (the only per-format code)
| Substrate | render | address (stroke → target) | apply patch |
| :--- | :--- | :--- | :--- |
| **Markdown** | md → HTML | DOM `data-src-range` (renderer source maps) → block line range | rewrite source lines |
| **Mermaid** | mmd → SVG | SVG node/edge IDs (`getBoundingClientRect`) → Mermaid source node | rewrite Mermaid text |
| **Excalidraw** | scene → canvas | element IDs projected to screen via current camera | `updateScene` ops |

One shared interpreter above; three thin adapters below.

### Pointer-event routing (the one piece of real plumbing)
A transparent top layer would swallow all clicks. A **draw-mode toggle** controls it:
- **draw mode**: overlay `pointer-events: auto` — captures freehand.
- **otherwise**: overlay `pointer-events: none` — events pass through so the user can
  still select/pan the Excalidraw canvas or scroll the Markdown.

---

## Diagram Engines — Mermaid default, Excalidraw promotion

Both engines are kept, structured as a **one-way lifecycle**, not two parallel systems:

- **Mermaid is the default.** Text, LLM-friendly, diffable, git-clean, already built.
  Born from voice ("draw a flowchart of X") — the model emits Mermaid directly. Most
  diagrams never leave this stage.
- **Excalidraw is an opt-in promotion** for freehand / spatial work. When the user wants
  to sketch or hand-rearrange, convert Mermaid → Excalidraw (via the
  `mermaid-to-excalidraw` importer). From then on, that diagram's source of truth is
  Excalidraw JSON.

**Promotion is one-directional** (Mermaid → Excalidraw; never back — it's lossy). The
cost accepted consciously: once promoted, that diagram leaves the text-diffable world
(history becomes Excalidraw JSON blobs). Therefore: **stay in Mermaid as long as possible;
promote only on explicit freehand intent.**

The freehand overlay + resolver is the *trigger* for promotion and the *input* for editing
once promoted — the same component in both cases.

---

## Excalidraw stage — editing detail

This section applies once a diagram is in the Excalidraw stage. It is preserved from the
prior Excalidraw design work because the editing contract is sound.

### Library choice: Excalidraw (`@excalidraw/excalidraw`, MIT)
- **MIT-licensed** — free to embed/modify/ship, no watermark (tldraw's SDK needs a
  commercial license / watermark). Decisive factor.
- **Flat, diffable JSON** — each element `{id, type, x, y, width, height, …}`; arrows bind
  via `startBinding`/`endBinding`. Heavily represented in LLM training data.
- **Ecosystem**: `mermaid-to-excalidraw` (the promotion path), AWS/GCP icon libraries,
  reference MCP servers for live canvas editing.
- **Costs**: no first-party AI kit (we hand-roll serialize/validate/translate); fewer
  native shapes (no hexagon/cloud/cylinder — approximate or use a web-image icon); a React
  build step for the editor bundle.

### App-owned command language (`DiagramCommand[]`)
Models **never emit raw Excalidraw records.** They emit a small, validated, app-owned
command list; the backend validates it and the client translates to `updateScene` calls.
This gives validation, stability across Excalidraw upgrades, safe rollback, and keeps the
canvas library swappable.

The Excalidraw scene (`elements[]` + `appState`, with app ids in `customData`) is the
single source of truth; the semantic node/edge view is *derived* from it (no parallel graph
that can desync on manual edits).

| Op | Fields | Purpose |
| :--- | :--- | :--- |
| `create_node` | `id`, `label`, `shape?`, `x?`, `y?`, `style?` | New labeled node. |
| `set_shape` | `id`, `shape` | Change shape. |
| `set_style` | `id`, `style{color?,fill?,stroke?,size?}` | Visual style. |
| `rename` | `id`, `label` | Change node text. |
| `move` | `id`, `x`, `y` | Reposition. |
| `connect` | `id`, `from`, `to`, `label?` | Bound arrow (`startBinding`/`endBinding`). |
| `disconnect` | `id` | Remove a connection. |
| `delete` | `id` | Remove a node or connection. |
| `set_image` | `id`, `query`\|`fileId` | Replace node visual with a web-searched icon. |
| `add_image` | `id`, `query`\|`fileId`, `x`, `y` | Place a standalone image. |
| `add_annotation` | `id`, `kind`, `x`, `y`, `text?`, `shape?` | Freeform escape hatch. |

- **`shape`** ∈ { rectangle, ellipse/circle, diamond, arrow, line, text, image }. Shapes
  Excalidraw lacks are mapped to the nearest or to a web-image icon.
- **`style`** maps to Excalidraw props (`strokeColor`, `backgroundColor`, `fillStyle`, …).

### Apply via id-keyed ops, then materialize
The model emits ops keyed by **id**, not full scene format:
```json
{ "ops": [
  { "op": "add",    "element": { "id": "db1", "type": "rectangle", "x": 420, "y": 80 } },
  { "op": "update", "id": "api1", "props": { "x": 420 } },
  { "op": "delete", "id": "stroke_7f2" }
] }
```
**Pipeline:** model emits ops → harness **validates** (ids exist, bindings resolve) →
harness **materializes** (applies to current scene, fills boilerplate — `seed`,
`versionNonce`, two-way `boundElements`/binding bookkeeping) → `updateScene({elements})`.

Why not have the model emit `updateScene` directly? That forces it to regenerate the whole
scene every turn (burning tokens, risking clobbering user elements, coupling prompts to a
library API). Ops keep validation between the model and the canvas, keep correctness
details in code, and keep the prompt layer canvas-agnostic (portable to tldraw later).

### Model input: image + slimmed projection
Send the **PNG export** + a **slimmed scene projection** (`{id, type, x, y, w, h, text?,
bindings?}`; freedraw → `{id, bbox}`), *not* raw JSON. Image = semantics of messy input;
projection = addressability of output. Neither alone works.

### Sketch cleanup: replacement mapping
"Clean up my freehand" falls out of the diff model: the model **adds** clean shapes and
**reports which input stroke ids each replaces**, so the harness deletes exactly those —
no ghost scribbles accumulate.

### Excalidraw API surface (pin the version in the Phase-2 spike)
- `@excalidraw/excalidraw` (MIT), React component + imperative `excalidrawAPI` ref.
- Apply: `updateScene({elements, appState})`; read: `getSceneElements()`/`getAppState()`;
  images: `addFiles()`.
- App data: `customData`. Selection: `appState.selectedElementIds`.
- Arrow binding: `startBinding`/`endBinding` + `boundElements`.
- Export: `exportToSvg`, `exportToBlob` (PNG), `serializeAsJSON`.

---

## Storage & Git Integration

Single git repo at `VOICE_COCKPIT_GIT_ROOT`; one folder per project. Saves are commits,
pushed to `origin` (push failure warns, never blocks the local commit). Git history *is*
the version history — no `version_N/` dirs, no copy-on-write fork.

```text
VOICE_COCKPIT_GIT_ROOT/              # single git repo
  <slug>/                            # one folder per project
    <slug>.md                        # doc markdown; references current diagram renders
    transcript.md
    speakers.json
    metadata.json
    .session/                        # uncommitted drafts + snapshot_refs (git-ignored)
    images/                          # web-searched icons
    diagrams/
      <diagramID>/
        # Mermaid stage: the Mermaid source lives inline in <slug>.md.
        # Excalidraw stage (post-promotion): structured artifacts per edit —
        <name>-<diagramID>-v0.excalidraw   # scene JSON — source of truth
        <name>-<diagramID>-v0.svg          # embedded render
        <name>-<diagramID>-v0.png          # raster for the VLM
        <name>-<diagramID>-v1.excalidraw
        ...
```

- **Snapshots vs. commits:** a `snapshot_ref` is a temporary PNG/SVG export — VLM input
  only, lives in `.session/` (git-ignored), never committed. A `v<k>` is an accepted state
  persisted as a git commit.
- **Undo:** `revert_diagram` restores the previous `v<k>` (re-points the render link,
  reloads the source); the revert is itself a commit. The numbered `v<k>` artifacts give
  fast in-session undo; git is the durable cross-session record.
- **Embedding:** the doc markdown references the current render via a standard image link;
  the embedded SVG and the VLM's PNG are the same rendering of the same state.

---

## AI Interpretation — models & routing

### Capability slots (no pinned model names)
| Slot | Capabilities | Notes |
| :--- | :--- | :--- |
| `controller_model` | tool calling, low latency, text | The voice orchestrator; the only writer. |
| `vision_model_fast` | image input, structured JSON, low latency | Default interpretation pass. |
| `vision_model_quality` | strong spatial reasoning | Fallback for hard sketches. |
| `summarization_model` | cheap text | Transcript / content synthesis. |

- **Routing — cheap vs. expensive:** unambiguous, named edits (recolor, rename, move a known
  element; "move the Architecture section above Overview") → **Controller-only**, no vision.
  Interpretive/spatial input (freehand sketches, "turn this squiggle into X") → **vision pass**.
- The Controller is the single brain and single writer (no two-agent races). Vision always
  receives **both** the snapshot and the spoken intent together.
- **Vision is opt-in:** an explicit gate precedes any snapshot leaving for the VLM.
- No image-generation model — we want validated *patches/commands*, not pixels back.

### Deterministic-first principle
For the freehand resolver, the **deterministic hit-test path** (stroke geometry →
substrate source-map/IDs → target, no LLM) should handle the common cases (circle/arrow/
underline on clear blocks/nodes). The VLM is reserved for genuinely ambiguous spatial
sketches. This inverts the cost model favorably — most gestures resolve without a model call.

---

## Cross-Cutting Concerns

- **Structured truth, never SVG.** All edits target Markdown / Mermaid / Excalidraw JSON;
  SVG/PNG are regenerated renders.
- **Disposable intent.** Overlay strokes are never persisted or committed; they resolve to
  a patch, then clear.
- **Command/patch validation.** Validate every model-produced patch against the schema
  before applying; reject dangling references and re-prompt — never apply blind.
- **Patches, not rewrites.** Edits are diffs to the structured source, keeping history
  clean and auditable (one commit per accepted edit).
- **Prompt injection / content trust:** treat scribbled text and document contents as
  **data, not instructions**.
- **Path & workspace safety:** all artifacts under `VOICE_COCKPIT_GIT_ROOT`, sanitized
  names, block traversal.
- **State machine:** reuse `doc_state.py` (`shell` ↔ `doc_mode` ↔ `diagram_focus`).
  Diagram commands require `doc_mode`; exiting `diagram_focus` leaves the voice session intact.
- **Mobile-first:** freehand + voice is the target; it works on a phone.

---

## Delivery Phases (proposed — sequence under revision)

> The build order below proves the architecture on the **cheapest, highest-addressability
> substrate first** (Markdown, then Mermaid SVG) before committing to the Excalidraw editor.
> The freehand overlay + resolver is built once and reused. (This sequence is a proposal;
> the user is refining the implementation order.)

### Phase A — Voice document/diagram editing (largely done)
- [x] Voice → Markdown generation and follow-up edits.
- [x] Mermaid diagrams via voice (`insert_diagram`/`update_diagram`/`move_diagram`).
- [ ] `move_section(section, before|after)` — voice-driven section moves (reuses
      `_find_section`/`_replace_section`; mirrors `move_diagram`).

### Phase B — The freehand overlay + resolver (the new frontier)
- [ ] Transparent **screen-space** capture overlay component (reusable), with the
      **draw-mode pointer-event toggle**.
- [ ] **Markdown adapter:** render with `data-src-range` source maps; hit-test stroke →
      block; gesture → patch (`move_section`, `edit_doc`, delete).
- [ ] **Resolver spike (do first):** throwaway page — draw an arrow over rendered Markdown,
      print `{verb, from_block, to_block}`. Validates the deterministic stroke→block path
      *before* wiring voice/VLM. This is the make-or-break mechanic.
- [ ] **Mermaid adapter:** hit-test stroke → SVG node ID → Mermaid source node.

### Phase C — Excalidraw stage (the big lift, only when freehand is proven)
**C1. API spike (do first):** mount `@excalidraw/excalidraw`, round-trip
`serializeAsJSON`/`updateScene`, bind an arrow, export PNG+SVG — **pin the version.**
**C2. Build/deploy:** package + lockfile, `editor.html` build pipeline, FastAPI static
route, dev workflow.
**C3. Editing core:** `editor.html` ↔ `cockpit.html` `postMessage` (request ids + timeouts);
backend command **validator** + JS **translator**; `create_diagram` + `update_shapes`;
selection routing; persistence of `v<k>` via `git_storage` + `.session/` drafts.
**C4. Promotion:** Mermaid → Excalidraw via `mermaid-to-excalidraw`, triggered by freehand
intent.

### Phase D — The vision loop (only after local editing is solid)
- [ ] `generate_snapshot` (client export → `snapshot_ref`, request id/timeout).
- [ ] Opt-in gate before `interpret_sketch`.
- [ ] `interpret_sketch`: snapshot PNG + voice/intent + selection + slim projection →
      `vision_model_fast` (escalate to `_quality`) → commands → validate → apply → commit.
- [ ] Web-image nodes: `search_image` + `set_node_image` (reuse DuckDuckGo image infra).

---

## Tool-Call Contracts (Excalidraw stage)

| Tool | Required | Optional | Errors |
| :--- | :--- | :--- | :--- |
| `enter_diagram_focus` | `diagram_id` | — | `ID_NOT_FOUND`, `INVALID_STATE` |
| `exit_diagram_focus` | — | `discard` | `NOT_ACTIVE`, `SAVE_FAILED` |
| `create_diagram` | `diagram_id`, `title` | `position_hint` | `ID_COLLISION`, `WRITE_ERROR` |
| `update_shapes` | `diagram_id`, `commands` | `expected_version` | `INVALID_COMMANDS`, `STALE_VERSION`, `ID_NOT_FOUND` |
| `generate_snapshot` | `diagram_id`, `request_id` | — | `EXPORT_FAILED`, `EXPORT_TIMEOUT`, `NOT_IN_DIAGRAM` |
| `interpret_sketch` | `diagram_id`, `snapshot_ref`, `intent` | `scene_json` | `CONSENT_REQUIRED`, `VISION_UNAVAILABLE`, `MODEL_ERROR`, `INVALID_COMMANDS` |
| `revert_diagram` | `diagram_id` | `steps` (=1) | `NOTHING_TO_REVERT` |
| `search_image` | `query` | `count` | `SEARCH_ERROR`, `NO_RESULTS` |
| `set_node_image` | `diagram_id`, `node_id`, `image_ref` | — | `ID_NOT_FOUND`, `WRITE_ERROR` |

- `expected_version` gives optimistic concurrency (`STALE_VERSION` on mismatch).
- `exit_diagram_focus(discard=true)` discards uncommitted draft, reverts to last `v<k>`.

---

## Open Issues / Spikes

- [ ] **🔬 Resolver reliability (Phase B, highest-risk for the new layer):** can a stroke be
      reliably resolved to the right source block/node deterministically? Spike on Markdown
      first (`data-src-range` hit-test), then Mermaid SVG. Make-or-break for freehand intent.
- [ ] **🔬 Vision reliability (Phase D):** can a VLM turn scribbles + speech into correct
      diagram commands? Harness: `spikes/vision_spike.py` (throwaway) — hand-drawn PNG +
      intent → validate returned `DiagramCommand[]` (schema, faithfulness, latency, tokens).
      Cloud bake-off via OpenRouter (`google/gemini-2.5-pro`, `anthropic/claude-sonnet`,
      `qwen/qwen2.5-vl-72b`, `mistralai/pixtral-large`); local Qwen2.5-VL 3B 4-bit as an
      offline helper tier (7B OOMs on an 8 GB Air). Results: **TBD.**
- [ ] **Pointer-event routing UX:** how the draw-mode toggle is triggered by voice/gesture
      without swallowing normal interaction.
- [ ] **Promotion UX:** when/how Mermaid → Excalidraw promotion is offered and confirmed;
      what happens to the inline Mermaid source after promotion.
- [ ] **Shape-ceiling policy:** Excalidraw lacks hexagon/cylinder/cloud/star — approximate,
      web-image icon, or reject, per shape.
- [ ] **Layout ownership:** keep sketched coords vs. auto-layout (dagre/elk) vs. model hints.
- [ ] **Web-image quality:** DuckDuckGo rate-limits / mixed licenses — confirm icon quality
      or add a curated fallback set.
- [ ] **Round-trip latency:** measure capture → resolve → (vision) → apply → re-render.

---

## Appendix — Excalidraw vs tldraw (decision record)

Both are shapes-with-ids + coordinates + bindings, but **not compatible**.
- **Format:** Excalidraw = flat element array, open `.excalidraw` JSON, trivially diffable,
  heavy LLM prior art. tldraw = normalized record store with schema migrations — more
  powerful for collaborative apps, but overhead here and less LLM prior art.
- **Licensing (decisive):** Excalidraw is **MIT**; tldraw's SDK requires a "made with
  tldraw" watermark unless commercially licensed.
- **Reversibility:** the model-facing interface is our **ops vocabulary + slimmed
  projection**, not the raw format. Migrating canvases later = rewrite only the thin
  translator; prompts, voice pipeline, and the interpretation loop survive.
- **Prior art (JSON emission largely solved):** Excalidraw's built-in text-to-diagram,
  `coleam00/excalidraw-diagram-skill`, `yctimlin/mcp_excalidraw` (live incremental editing —
  closest to our pattern), `awesome-copilot`'s generator + AWS icon set. Our novel pieces
  are the **voice channel** and the **freehand-overlay intent loop**.
