# Voice-Driven Documentation and Diagramming Plan (Excalidraw-Native)

## Overview

This plan covers the **interactive diagramming layer** of the Voice Coding Cockpit's Documentation Mode, using an **Excalidraw-native** canvas with **Dual LLM** coordination (Controller + Vision) to bridge voice, sketch, and polished diagrams.

**Scope.** Documentation Mode itself — voice-driven markdown editing, the chronological transcript, speaker attribution, and the versioned `version_N/` storage model — is **already implemented (Phases 0–1)** and is unchanged by this plan. This document specifies only the new diagramming layer that replaces the previous Mermaid implementation. Review Mode is **dropped** (versioned edits capture review intent).

The version's markdown file is named `<slug>.md` in the implementation; this document calls it the **doc markdown** for brevity.

**Retained from Documentation Mode (Phases 0–1, implemented):** Deepgram **diarization** plus **contributor confirmation** — when a new voice is detected the Controller asks who it is and records the mapping via `set_speaker_name` (persisted in `speakers.json`), attributing each transcript utterance to a named speaker. Diagram edits are attributed to whoever requested them.

**Accepted decisions (2026-06-10):**
- **Excalidraw replaces Mermaid** as the diagramming engine. **Excalidraw chosen over tldraw because it is MIT-licensed** (fully open, embeddable, no watermark/license key); tldraw's SDK requires a commercial license / shows a watermark. **No Mermaid backward compatibility** (legacy Mermaid blocks render as an explicit placeholder, never silently blanked).
- **App-owned command language** — the Vision/Controller models emit a small, validated **hybrid** command set (semantic graph ops + shape/style control), not raw Excalidraw element records; the client translates to Excalidraw API calls. The Excalidraw scene (with `customData`-tagged ids) is the **single source of truth**. This is the central safety contract — and it makes the canvas library a swappable backend.
- **Two-part vision input** — the model always receives **both the user's scribbles (snapshot image) and what the user is saying (intent transcript)**, plus a **slimmed scene projection** (not raw Excalidraw JSON). Image = semantics of messy input; projection = addressability of output.
- **Apply via id-keyed ops, not JSON Patch** — models emit `add`/`update`/`delete` keyed by element **id**; the harness validates each (id exists, bindings resolve) and applies via `updateScene`. RFC-6902 array-index paths are too brittle — one off-by-one corrupts the scene.
- **Sketch cleanup tracks replacements** — when polishing freehand, the model reports which **input stroke ids each clean shape replaces**, so the originals are deleted (or faded pending confirmation) and the canvas never accumulates ghost scribbles.
- **The canvas choice is cheaply reversible** — the model-facing interface is our **ops vocabulary + slimmed projection**, not the raw format. Migrating to another canvas later = rewrite only the thin translator; prompts, voice pipeline, and loop architecture survive.
- **Selection is the universal pointer** — native Excalidraw selection (`appState.selectedElementIds`) resolves "this" (no gesture pens/colors); the selected id is passed to both the Controller and the Vision model.
- **Rolling local capture buffer** — recent `(snapshot, voice-fragment)` pairs are buffered locally and the relevant one is sent on-demand; capture is automatic/free, sending stays on-demand.
- **Web-image nodes** — a node's visual can be a web-searched icon (e.g. "S3", "cloud"), reusing the existing image-search infrastructure (Excalidraw `image` element + `files`).
- **Client-side export** of the canvas (no server-side headless renderer, no filesystem watcher).
- **Single source of truth** = the live Excalidraw scene; persistence at explicit save points; drafts autosave to `.session/` for crash/reload recovery.
- **Snapshots are inputs, versions are commitments** — a Vision snapshot is a temporary `snapshot_ref`; only an *accepted/applied* edit advances the committed `v<k>`.
- **Diagram edits honor document copy-on-write** — re-pointing an SVG link mutates `<slug>.md`, so editing a diagram in an opened existing version **forks to a new `version_N` first** (same rule we already enforce for text edits).
- **Vision is opt-in** — the Controller confirms before any snapshot leaves for the Vision model.
- **Mobile-first interaction**: freehand sketching + voice is the target; it works on a phone.

---

## Part 1 — Feature Specifications

### Diagram Workflow (Multimodal)

1. **Talk & Sketch**: The user discusses architecture while scribbling rough ideas on an infinite Excalidraw canvas (freehand + voice; works on mobile).
2. **Generate Trigger**: The user says *"can you generate the image"* (or the Controller proposes it at a point of visual complexity).
3. **Opt-in gate**: The Controller confirms sending the canvas to the Vision model. Nothing leaves without this.
4. **Client Export (silent)**: The browser exports the current canvas — scribbles + existing elements — to PNG (for vision) and SVG (for embedding) **in-memory** via Excalidraw's `exportToBlob()` / `exportToSvg()`, producing a temporary **`snapshot_ref`** (not a committed version). No flicker, no backend renderer.
5. **Vision Pass**: Both inputs together — the `snapshot_ref` PNG (**user scribbles**) **and** the recent **voice/intent transcript** — plus the current scene JSON are sent (via the backend) to the **Vision LLM**, which returns a list of **validated diagram commands** (see schema below) — not raw Excalidraw records.
6. **Validate & Apply**: The Controller validates the commands; the client translates them to Excalidraw API calls (`updateScene`) and applies them. On acceptance this **commits a new diagram version `v<k>`** (forking the document version first if needed).
7. **Iterative Refinement**: The Controller chooses the cheap **direct** path (emits commands itself, no vision) for simple named edits, or another **vision** pass for ambiguous/spatial ones. The user can say *"undo"* / *"go back"* to revert to the previous version.
8. **Auto-Embedding**: The accepted version's SVG (PNG fallback) is embedded in the doc markdown via a standard image link.

### Cheap vs. Expensive Interactions
- **Direct (Controller only, no vision)**: simple, named edits — recolor, rename, move a known element. The Controller emits commands directly; no snapshot, no vision call.
- **Vision (Vision LLM)**: interpreting freehand sketches, ambiguous layout, "turn this squiggle into X". Invoked only on intent **and** opt-in.

---

## Part 2 — Architecture

### Library Selection

**Chosen stack: Excalidraw (`@excalidraw/excalidraw`, MIT).**
- **Why Excalidraw over tldraw?** Full comparison in **§ Excalidraw vs tldraw** below — chiefly its MIT license, flat diffable JSON, and heavy LLM prior art.
- **Why drop Mermaid?** Too rigid — prevents messy sketching and the relative spatial commands ("move this over there") a voice tool needs.
- **No Mermaid backward compatibility** (with a UX placeholder, not silent loss).

> **Costs of this choice (honest):**
> - **No first-party AI/agent kit.** tldraw ships an agent starter kit (schema + validator + appliers); Excalidraw does not. So we **hand-roll the serialize/validate/translate layer** — which is exactly why the app-owned command language matters.
> - **Fewer native shapes.** Excalidraw elements are `rectangle`, `diamond`, `ellipse`, `arrow`, `line`, `freedraw`, `text`, `image`, `frame` — **no native hexagon/cloud/cylinder/star/triangle.** Richer shapes are approximated (e.g. cylinder→rectangle/ellipse) or out of scope.
> - **React build step** for `editor.html` (same as any real canvas) — a departure from the current "static HTML, no build step." Build/deploy tasks are explicit in Phase 2.

### Three-Layer Architecture (Vision + LLM + Tool Abstraction)

Based on research into deployed vision-LLM systems and multimodal iteration patterns, the diagram generation pipeline is structured as three **decoupled layers** that can be evolved independently:

#### Why Three Layers?

Two primary reasons:

**1. Debuggability — Isolate failures to the layer that broke**

When the output is wrong, explicit intermediate layers let you diagnose *which* model or stage failed:
- If the diagram layout is nonsensical → inspect Layer 1 (vision feedback)
- If the ops are semantically broken → inspect Layer 2 (LLM)
- If valid ops don't translate correctly → inspect Layer 3 (translator)

Without this separation, a bad diagram doesn't tell you whether the vision model misread the canvas, the LLM ignored the spatial feedback, or the translator mishandled the ops—**you must guess**.

**Prior art:** Compiler design (lexer → parser → codegen) and **program synthesis research** (e.g., CoDex, MTPG) show that intermediate representation languages dramatically improve debuggability—you can inspect what the synthesizer thinks the spec means *before* trying to implement it. **Visual Chain-of-Thought (VCoT)** papers (e.g., arXiv:2403.14401) empirically show intermediate reasoning steps improve both accuracy *and* interpretability. **LangGraph** and production LLM systems (Anthropic's Constitutional AI, DeepSeek's reasoning traces) rely on explicit intermediate steps for observability and debugging.

**2. Tool Swapping — Future-proof against drawing library changes**

The ops contract isolates tool-agnostic knowledge (vision, semantics) from tool-specific boilerplate (Excalidraw JSON format, tldraw store schema, SVG syntax). If you need to swap to tldraw, Figma API, or SVG:
- Vision and LLM prompts remain unchanged (they don't know about the tool)
- Only the translator layer changes (map ops to the new tool's API)

This is not just convenience — it's **reversible architecture**. If Excalidraw's licensing, dependencies, or feature set become problematic later, you're not locked in.

**Prior art:** **Stable Diffusion's AUTOMATIC1111 WebUI** explicitly separates conditioning logic, sampling strategy, and rendering so each can be swapped independently without touching the others. **Unix command-line philosophy** — each tool does one thing well and outputs a format the next tool consumes — has proven this pattern for 50+ years. **Compiler IRs** (intermediate representations) like LLVM enable retargeting to new backends without rewriting the front end.

**Combined:** These two goals (debuggability + portability) are achieved by making each layer's contract explicit: define what each layer takes in, what it outputs, and *what it must not know*.

#### Layer 1: Vision Model (Spatial Grounding)

**Input:**
- Previous canvas PNG (the old state)
- Current canvas PNG (what the user just drew)
- Selection info (which element is selected — resolves "this" in voice commands)
- Voice intent transcript (e.g., "make this pink", "move this here")

**Does NOT need:**
- Knowledge of Excalidraw JSON, tldraw store, SVG, or any tool format
- Knowledge of the scene's internal structure or element IDs

**Output:**
- Spatial feedback in natural language: *"move it 50px to the left", "make it bigger", "this overlaps the box on the right"*

**Remains unchanged when:** Swapping to a different drawing tool (tldraw, SVG, Figma API, etc.)

**References:**
- **HiViG** (Google Research): History-aware visually grounded agents use prior screenshots + visual feedback loops to prevent hallucination drift
- **SAM-Flow** (arXiv:2606.06120): Semantic region detection for delta-based editing with visual grounding
- **Stable Diffusion WebUI**: Image-to-image loopback and parameter persistence enable iterative visual refinement

#### Layer 2: LLM (Semantic Interpretation & Tool-Agnostic Ops)

**Input:**
- Vision model's spatial feedback (natural language: "move left", "bigger", etc.)
- Current scene JSON (tool-agnostic: `{id, label, shape, x, y, ...}` — same format regardless of underlying tool)
- Voice intent (higher-level semantic goal: "restructure this as an arch diagram")
- Previously issued ops (for consistency and constraint consolidation)

**Does NOT need:**
- To understand pixels or spatial layout (vision handled that)
- Tool-specific knowledge (Excalidraw JSON structure, tldraw bindings, SVG syntax, etc.)

**Output:**
- **Tool-agnostic ops** (same format for any drawing tool): `create_node`, `move`, `set_shape`, `set_style`, `connect`, `delete`, `add_annotation`

**Remains unchanged when:** Swapping to a different drawing tool

**References:**
- **Edit-R2** (arXiv:2606.05950): Multi-turn in-context editing consolidates scattered historical constraints before each turn — LLM receives full history of vision feedback + prior ops, which reduces drift
- **Chameleon** (Meta, arXiv:2405.09818): Early-fusion multimodal model demonstrates seamless handling of mixed-modality prompt sequences (image + text iteratively)
- **Visual Chain-of-Thought (VCoT)** (arXiv:2403.14401): Intermediate reasoning steps (e.g., LLM outputs explicit spatial interpretation before ops) improve both accuracy and debuggability

#### Layer 3: Translator (Tool-Specific Adapter)

**Input:**
- Tool-agnostic ops (from LLM)
- Current scene in the specific tool's format (Excalidraw JSON, tldraw store, SVG DOM, etc.)

**Output:**
- Tool-specific mutations applied to the tool's native API or format

**Changes when:** Swapping to a different drawing tool
- Excalidraw: `materialize_ops()` → Excalidraw elements with seed/versionNonce/bindings
- tldraw: `materialize_ops()` → tldraw shapes/arrows with normalized store records
- SVG: `materialize_ops()` → SVG elements and path data
- Same ops input, different output per tool

**References:**
- **LLVM (Low-Level Virtual Machine)**: IR-to-backend translation is the canonical model for portability — same IR, multiple targets (x86, ARM, RISC-V, etc.)
- **AUTOMATIC1111 WebUI (Stable Diffusion)**: Rendering layer is decoupled from sampling — can swap PNG vs WebP vs TIFF output without touching the diffusion logic
- **Compiler intermediate representations (MLIR, Cranelift)**: Abstractly, the translator layer is an "intermediate representation" compiler that targets drawing tools instead of CPUs

#### Tool Swapping

To migrate from Excalidraw to tldraw (or any other tool):

```
Vision prompt:    [UNCHANGED — still outputs spatial feedback]
LLM prompt:       [UNCHANGED — still outputs tool-agnostic ops]
Translator:       [CHANGE — map ops to tldraw API instead of Excalidraw JSON]
Scene JSON:       [FORMAT CHANGE — use tldraw's store model instead of Excalidraw]
```

The ops contract is the firewall: if two tools support the same op semantics, the upstream (vision + LLM) layers are portable.

---

### Excalidraw vs tldraw

Both are conceptually similar — shapes-with-ids, coordinates, and bindings — but they are **not compatible**, and they differ in ways that matter here.

**Format model.**
- **Excalidraw** — a **flat array of elements in plain JSON**. Each element is one object `{id, type, x, y, width, height, …}`; arrows reference shapes via `startBinding`/`endBinding`. That's essentially the whole mental model: an open, documented `.excalidraw` format, **trivially diffable**, and **heavily represented in what LLMs have learned to emit**. Our ops vocabulary maps onto it directly.
- **tldraw** — a **normalized record store**: shapes, bindings, pages, assets, and camera as separate typed records with schema versions and migrations. More sophisticated (better for collaborative apps / complex state), but that sophistication is **overhead** for us — more schema for the model to get right, more boilerplate to manage, less LLM prior art targeting it. tldraw's programmatic Editor API is genuinely nicer, but we don't need deep API power for "apply a list of add/update/delete ops."

**Licensing.** Excalidraw is **MIT**. tldraw's SDK uses its own license that requires a **"made with tldraw" watermark** unless you buy a commercial license. For a personal project that may eventually ship or open-source, **MIT is the frictionless choice** — the decisive factor.

**Ecosystem fit.** `mermaid-to-excalidraw`, the AWS/GCP architecture **icon libraries**, and a **live-editing MCP server** are all Excalidraw-side assets we can borrow on day one.

**Reversibility.** Cheaply reversible in the one place it matters: the **model-facing interface is our simplified projection + ops vocabulary, not the raw format**. If we ever migrate to tldraw, we rewrite the thin translation layer between our ops and the canvas API — the prompts, the voice pipeline, and the loop architecture all survive.

#### Prior art (the JSON-emission problem is largely solved)
A mini-ecosystem already emits valid Excalidraw JSON from LLMs — worth borrowing from:
- **Excalidraw's built-in "Text to diagram"** (open source): prompt → Mermaid (LLM) → Excalidraw via the `mermaid-to-excalidraw` converter. Useful reading even though we skip Mermaid.
- **coleam00/excalidraw-diagram-skill** (~3.4k★): NL → Excalidraw; notably implements the **render-and-verify loop** we chose to skip — a reference for when you *do* want it.
- **yctimlin/mcp_excalidraw** (~2k★): **most relevant** — live, interactive canvas editing; an agent applying **incremental changes to a running canvas** (our diff-application pattern, already demonstrated).
- **github/awesome-copilot's excalidraw-diagram-generator**: generates `.excalidraw` from NL (flowcharts, relationships, mind maps, architecture) and includes an **AWS architecture icon library**.
- **Agents365-ai/excalidraw-skill**, **ahmadawais/excalidraw-cli**: more of the same; the CLI's docs are a compact reference for the element format (arrow bindings, labels, zones).

**Takeaway:** LLMs emit valid Excalidraw JSON reliably enough that this whole crowd exists. **Our novel pieces are the voice channel and the sketch-input-on-the-same-canvas loop** — the JSON-emission part is largely solved.

### Intermediate Diagram Command Language (`DiagramCommand[]`) — central contract

The Vision and Controller models **never emit raw Excalidraw element records.** They emit a small, app-owned command list that the backend validates and the client translates to Excalidraw API calls (`updateScene`). This gives validation, stability across Excalidraw upgrades, safer rollback, better model accuracy, and keeps the canvas library swappable.

The language is **hybrid**: **semantic graph** operations *and* **shape/style** control, so both *"connect the auth service to the database"* and *"make the database a circle"* are expressible.

> **Two-part input.** The model always receives **both the user's scribbles (snapshot image) and what the user is saying (voice/intent transcript)** — plus the current scene JSON. The pixels show layout/rough shapes; the speech disambiguates intent and naming.

#### Model
A diagram is **nodes** (labeled shapes), **connections** (arrows bound between nodes), and freeform **annotations**. Every element has a stable app **`id`**, stored in the Excalidraw element's **`customData`** field. **The Excalidraw scene (`elements[]` + `appState`) is the single source of truth**; the semantic node/edge view is *derived* from it (no parallel graph that can desync when the user edits directly).

#### Commands

| Op | Fields | Purpose |
| :--- | :--- | :--- |
| `create_node` | `id`, `label`, `shape?`(=rectangle), `x?`, `y?`, `style?` | New labeled node. |
| `set_shape` | `id`, `shape` | Change a node's shape — *"make the database a circle."* |
| `set_style` | `id`, `style{color?,fill?,stroke?,size?}` | Visual style — *"make it blue."* |
| `rename` | `id`, `label` | Change a node's text. |
| `move` | `id`, `x`, `y` | Reposition. |
| `connect` | `id`, `from`, `to`, `label?` | Create a **bound** arrow (binds via `startBinding`/`endBinding`). |
| `disconnect` | `id` | Remove a connection. |
| `delete` | `id` | Remove a node or connection. |
| `set_image` | `id`, `query`\|`fileId` | Replace a node's visual with a web-searched icon (Excalidraw `image` element). |
| `add_image` | `id`, `query`\|`fileId`, `x`, `y` | Place a standalone web-searched image. |
| `add_annotation` | `id`, `kind`(text\|shape\|draw), `x`, `y`, `text?`, `shape?` | Freeform escape hatch. |
| `group` *(later)* | `id`, `members[]`, `label?` | Optional frame/group. |

#### Vocabularies (constrained, validated — Excalidraw-native)
- **`shape`** ∈ { `rectangle`, `ellipse`/`circle`, `diamond`/`rhombus`, `arrow`, `line`, `text`, `image` } → Excalidraw element types. **Requests for shapes Excalidraw lacks** (hexagon, cloud, cylinder, star, triangle) are **rejected or mapped to the nearest** (e.g. cylinder→rectangle); a node's visual can also be a **web-searched image** (see `set_image`) for icons Excalidraw can't draw.
- **`style`** maps to Excalidraw element props — `strokeColor`, `backgroundColor`, `fillStyle`, `strokeWidth`, font. Map free-text colors to the nearest supported value.

#### Examples
*"Make the database a circle"*:
```json
{ "commands": [ { "op": "set_shape", "id": "database", "shape": "ellipse" } ] }
```
Build a structure from a sketch (note: DB rendered as rectangle since Excalidraw has no cylinder):
```json
{ "commands": [
  { "op": "create_node", "id": "auth", "label": "Auth Service", "shape": "rectangle", "x": 120, "y": 80 },
  { "op": "create_node", "id": "db",   "label": "User DB",      "shape": "rectangle", "x": 420, "y": 80 },
  { "op": "connect", "id": "e1", "from": "auth", "to": "db", "label": "reads/writes" }
] }
```

#### Rules
- **Stable ids**: models receive the current scene (ids + labels) and must reuse existing ids; the validator rejects dangling references and duplicates. App ids live in element `customData`.
- **Scene = source of truth**: the derived semantic view is read from `elements[]`, so manual canvas edits never desync a parallel model.
- **Arrows bind**: `connect` creates an Excalidraw `arrow` element with `startBinding`/`endBinding` (+ `boundElements` on the nodes) so it follows the shapes.
- **Atomic batches**: the whole `commands[]` is validated and applied in one `updateScene`, committing exactly one version. Any invalid command rejects the batch and re-prompts.
- **Coordinates are hints, not truth**: prefer the user's sketched positions; optional auto-layout (dagre/elk) for clean graphs; treat model `x`/`y` as hints.

#### Model input: image + slimmed scene projection
Send the **PNG export** of the canvas **and a simplified projection** of the scene — *not* raw Excalidraw JSON. Raw elements carry token-burning, model-useless fields (`seed`, `versionNonce`, `updated`, roundness internals, full freehand point arrays). Project each element down to ~`{id, type, x, y, width, height, text?, startBinding?, endBinding?}`; for freedraw strokes, just `{id, bbox}`. The **image** tells the model a stroke is "an arrow pointing at the cache box"; the **projection** tells it that stroke is `stroke_7f2` at those coords. Map the model's ops back onto full elements (filling boilerplate defaults) on our side. *Division of labor: image = semantics of messy input; projection = addressability of output. Neither alone works.*

#### Applying edits: id-keyed ops, not updateScene format
The model emits operations keyed by **id**, not raw Excalidraw scene format:
```json
{ "ops": [
  { "op": "add",    "element": { "id": "db1", "type": "rectangle", "x": 420, "y": 80 } },
  { "op": "update", "id": "api1", "props": { "x": 420 } },
  { "op": "delete", "id": "stroke_7f2" }
] }
```

**Why not ask the model for updateScene directly?** `updateScene` is Excalidraw's React API call expecting a complete `elements[]` array with all boilerplate fields populated: `seed`, `versionNonce`, `version`, roundness internals, full `boundElements` bookkeeping. Asking the model to emit that means asking it to **regenerate the entire scene every turn** — burning the tokens you saved with the slim projection, risking clobbering user elements it shouldn't touch, and coupling your prompts to a library API that can change.

**The pipeline:** model emits ops → harness validates (ids exist, bindings resolve) → harness **materializes** (applies ops to the current scene, fills boilerplate defaults for new elements) → `excalidrawAPI.updateScene({elements: newElements})`. Three concrete wins:
1. **Validation lives between ops and materialization** — exactly where you catch dangling-binding and phantom-id failures before they hit the canvas.
2. **Correctness details stay in code.** Incrementing `versionNonce` on updated elements, generating valid `seed`, maintaining the two-way `boundElements`/`startBinding` consistency that Excalidraw requires for arrows to stay attached — models get that bidirectional bookkeeping wrong constantly; a 20-line materializer never does.
3. **Reversibility holds.** Ops are canvas-agnostic, so the prompt layer survives any future migration (to tldraw or elsewhere).

**System prompt: few-shot examples.** In the `SYSTEM` string, show the model 2–3 few-shot examples of ops sequences (including one that interprets a freehand stroke into a clean shape and deletes the stroke as a replacement). That does more for output reliability than any amount of schema description.

#### Sketch cleanup: replacement mapping
"Clean up my freehand" falls out of the diff model: the model **adds** clean shapes and **deletes** (or fades, pending confirmation) the rough strokes it interpreted. To avoid accumulating ghost scribbles, the model must **report which input stroke ids each clean shape replaces**, so the harness removes exactly those originals.

#### Known issues / risks
- **Pixels → identity** (the real hard part): which drawn blob becomes a *node* vs. *annotation* — the vision spike targets this.
- **Shape ceiling is tighter on Excalidraw** (no hexagon/cylinder/cloud/etc.) — decide approximate-vs-image-vs-reject per shape.
- **Layout ownership**: don't trust model coordinates.
- **Binding correctness**: connections must set Excalidraw bindings or they break on move.
- **Desync**: avoided only because the scene (with `customData` ids) is canonical.

### Deixis, Selection & Snapshot Timing

Commands like *"make this a rhombus"* or *"make this red"* resolve **"this"** via **native Excalidraw selection** — no gesture pen or colors:
- The user **selects** an element; its `customData` app id is read from `appState.selectedElementIds` and included in context.
- **Selection is always in context** — sent to the Controller *and*, on a vision pass, to the Vision model (selected id + label + bounds).
- **Routing**: unambiguous edits on a selection → **Controller-only command** (no vision); interpretive edits ("turn this into an S3 icon", "clean up this region") → **Vision pass** with the selection as a hint.
- If **nothing is selected** and the user says "this", the Controller asks them to select (or falls back to a whole-canvas vision pass).

#### Rolling local capture buffer
The client keeps a small **in-memory rolling buffer** of recent `(snapshot, voice-fragment)` pairs — captured cheaply on pointer-up / every couple seconds, **locally, no network/model call.** When a command fires, the backend sends the buffered pair matching when the user was speaking. **Capture is automatic and local; sending is on-demand** — preserving the cost model while feeding vision the canvas *as it was when the user pointed and spoke.*

### Dual LLM Coordination

| Model slot | Role | Inputs | Outputs |
| :--- | :--- | :--- | :--- |
| **Controller** (voice) | Orchestrator, logic, safety | Voice transcript, document state, current scene JSON | Tool calls, user questions, direct **commands**, validate/apply/rollback |
| **Vision** (eyes) | Spatial interpretation | **Canvas snapshot (scribbles) + voice/intent transcript** + scene JSON | **Validated diagram commands**, layout advice |

The user converses with the **Controller** — the single brain that decides when to consult Vision and the only writer that applies commands (no two-agent races). The Vision model always sees **both** the scribbles and the spoken intent together.

#### Mediation logic (client-export flow)
1. **Trigger + opt-in**: Controller gets "generate the image" and confirms the vision opt-in.
2. **Client export**: Controller messages the browser with an **export request id**; `editor.html` exports PNG + SVG offscreen via Excalidraw utils and **POSTs the blobs to `/api/diagram/snapshot`**, returning a temporary `snapshot_ref`. Local (`localhost`) → sub-millisecond upload.
3. **Context packaging**: Backend packages the `snapshot_ref` PNG + recent voice context + scene JSON for the Vision LLM (keys stay server-side). **The snapshot is not a committed version.**
4. **Interpretation**: Vision LLM returns diagram **commands** + advice.
5. **Validate & apply**: Controller (code) validates against the schema; the client translates and applies via `excalidrawAPI.updateScene(...)`. **On acceptance**, the Controller forks the document version if needed (copy-on-write) and persists a new committed `v<k>` (`.excalidraw` + `.svg` + `.png`), then re-points the markdown link.

### Storage & Integration

#### Single source of truth + draft recovery
The **live Excalidraw scene** is canonical during a session. AI commands apply *into the scene*. Committed persistence happens at **explicit save points** (accepted AI edit, user "save", exit). Between those, the scene **autosaves to `.session/`** (uncommitted draft) so a mobile reload or dropped WebRTC session restores work without creating a version.

#### Snapshots vs. committed versions
- **`snapshot_ref`** — a temporary PNG/SVG export, *only* a Vision input; lives in `.session/`; never advances `v<k>`.
- **`v<k>`** — a committed, accepted diagram state. Only applied/accepted edits create one.

#### Diagram versioning (for undo) + copy-on-write
A committed version's filename is **`<name>-<diagramID>-v<k>`** (`<name>` = sanitized diagram title from `create_diagram`'s `title`; `<diagramID>` = unique slug). Each version has three artifacts (relevant files only — the version dir also holds the doc markdown, transcript, speakers, manifest, unchanged):

```text
version_N/
  <slug>.md                          # doc markdown; references the *current* SVG per diagram
  .session/                          # uncommitted drafts + snapshot_refs (never a version)
  images/                            # web-searched icons (Excalidraw image-element files)
  diagrams/
    <diagramID>/
      <name>-<diagramID>-v0.excalidraw  # scene JSON (elements+appState+files) — source of truth
      <name>-<diagramID>-v0.svg         # embedded in the doc markdown
      <name>-<diagramID>-v0.png         # raster sent to the Vision LLM
      <name>-<diagramID>-v1.excalidraw
      ...
```

- **Copy-on-write:** committing a diagram edit re-points the SVG link in `<slug>.md`, so editing a diagram in an **opened existing version forks to a new `version_N` first** — same rule as text edits. No historical version is mutated.
- **Undo:** `revert_diagram` restores the previous `v<k>` (re-points the link + reloads that `.excalidraw` via `updateScene`).
- **Scope:** image versioning is per diagram; document-level `version_N/` copy-on-write is the existing model, now also triggered by diagram commits.

#### Markdown embedding
- Standard link: `![System Architecture](./diagrams/auth-flow/arch-auth-flow-v3.svg)`
- The embedded SVG and the PNG sent to vision are **the same rendering of the same version** — display uses SVG, the model reads its PNG sibling.

#### Excalidraw API surface (verify in Phase 2 spike, pin the version)
- **Component/package**: `@excalidraw/excalidraw` (MIT), React component + imperative `excalidrawAPI` ref.
- **Apply**: `excalidrawAPI.updateScene({ elements, appState })`; read via `getSceneElements()` / `getAppState()`; images via `addFiles()`.
- **Per-element app data**: `customData` (our app ids/roles).
- **Selection**: `appState.selectedElementIds`.
- **Arrow binding**: `startBinding` / `endBinding` on the arrow + `boundElements` on shapes.
- **Export / serialize**: `exportToSvg`, `exportToBlob` (PNG), `serializeAsJSON` (the `.excalidraw` format).

### Capability Slots (no pinned model names)

| Slot | Required capabilities | Notes |
| :--- | :--- | :--- |
| `controller_model` | tool/function calling, low latency, text | The voice orchestrator (current text model from the dropdown). |
| `vision_model_fast` | image input, structured JSON output, low latency | **Default** vision pass — current Gemini Flash tier. |
| `vision_model_quality` | image input, strong spatial reasoning | **Fallback** for hard sketches — current Claude Sonnet tier. |
| `summarization_model` | cheap text | Transcript / main-content synthesis. |

- **Routing**: simple named edits → Controller only; first sketch interpretation → `vision_model_fast`; ambiguous/complex or rejected first pass → `vision_model_quality`.
- **No image-generation model** — we want validated *commands*, not pixels back.
- Each slot is overridable per environment.

### Tool-Call Contracts

| Tool | Required fields | Optional | Error responses |
| :--- | :--- | :--- | :--- |
| `enter_diagram_focus` | `diagram_id` | — | `ID_NOT_FOUND`, `INVALID_STATE` |
| `exit_diagram_focus` | — | `discard` | `NOT_ACTIVE`, `SAVE_FAILED` |
| `create_diagram` | `diagram_id`, `title` | `position_hint` | `ID_COLLISION`, `WRITE_ERROR` |
| `update_shapes` | `diagram_id`, `commands` | `expected_version` | `INVALID_COMMANDS`, `STALE_VERSION`, `ID_NOT_FOUND` |
| `generate_snapshot` | `diagram_id`, `request_id` | — | `EXPORT_FAILED`, `EXPORT_TIMEOUT`, `NOT_IN_DIAGRAM` |
| `interpret_sketch` | `diagram_id`, `snapshot_ref`, `intent` | `scene_json` | `CONSENT_REQUIRED`, `VISION_UNAVAILABLE`, `MODEL_ERROR`, `INVALID_COMMANDS` |
| `revert_diagram` | `diagram_id` | `steps` (default 1) | `NOTHING_TO_REVERT` |
| `search_image` | `query` | `count` | `SEARCH_ERROR`, `NO_RESULTS` |
| `set_node_image` | `diagram_id`, `node_id`, `image_ref` | — | `ID_NOT_FOUND`, `WRITE_ERROR` |

- `commands` is the app-owned command list (above); `update_shapes` is the cheap Controller-only path.
- `expected_version` gives optimistic concurrency: a mismatch returns `STALE_VERSION`.
- `generate_snapshot` returns a `snapshot_ref`; `interpret_sketch` consumes it and requires a prior opt-in (`CONSENT_REQUIRED` otherwise).
- `exit_diagram_focus(discard=true)` discards **uncommitted draft changes only** and reverts to the last persisted `v<k>`.
- `search_image` / `set_node_image` back the `set_image` / `add_image` commands — **reusing the existing DuckDuckGo image-search + select/size infrastructure** to fetch an icon and add it as an Excalidraw `image` element (file registered via `addFiles`, copied into the version's `images/` dir).

---

## Part 3 — Cross-Cutting Concerns

- **Command validation**: validate every model-produced command against the app schema **before** translating to Excalidraw API calls. Reject unknown/malformed/dangling-reference commands and re-prompt — never apply blind.
- **Rollback / undo**: each accepted edit commits a new `v<k>`; `revert_diagram` restores the previous (re-points the markdown link + reloads `.excalidraw`).
- **Draft recovery**: scene autosaves to `.session/` between commits so a mobile reload / dropped session restores in-progress work without creating a version.
- **Legacy Mermaid fallback**: when rendering a doc with old ```mermaid blocks, detect them and render a clear placeholder — *"Legacy Mermaid diagram — not supported in Excalidraw mode"* — preserving the source text; never silently blank.
- **Prompt injection / content trust**: treat scribbled text and document contents as **data, not instructions**.
- **Vision opt-in**: an explicit user opt-in gate precedes `interpret_sketch` (also in the workflow, step 3).
- **Path & workspace safety**: all diagram artifacts written under `VOICE_COCKPIT_DOCS_ROOT` with sanitized names; block traversal.
- **State machine**: reuses the existing `doc_state.py` transitions (`shell` ↔ `doc_mode` ↔ `diagram_focus`, `saving`, `error_recovery`). Exiting `diagram_focus` must leave the shell/voice session intact.

---

## Part 4 — Delivery Phases (Updated)

**Phases 0–1 are complete** (PoCs, versioned storage, mode state machine, `DocWriter`, Documentation Mode markdown/transcript/speaker handling). Phases below cover only the Excalidraw diagramming layer, in recommended build order.

### Phase 2 — Excalidraw Infrastructure & Local JSON Editing
**2a. API spike (do first):** in a throwaway page, mount `@excalidraw/excalidraw`, create/move/update elements, round-trip `serializeAsJSON`/`updateScene`, bind an arrow, and export PNG + SVG — **pinning the exact Excalidraw API + version.**
**2b. Build/deploy:**
- [ ] Package setup + lockfile policy (pin `@excalidraw/excalidraw` + React; upgrade procedure).
- [ ] Build pipeline for `editor.html`; defined **build output location**.
- [ ] **FastAPI static-serving route** for the built editor bundle.
- [ ] Dev workflow (build/watch) documented.
**2c. Editing core (hand-rolled — no first-party kit):**
- [ ] `editor.html` ↔ `cockpit.html` `postMessage` protocol **with request IDs + timeouts**.
- [ ] Persistence helpers for `diagrams/<diagramID>/...` (committed `v<k>`) and `.session/` drafts.
- [ ] **Validator** (Python, backend) for the command schema; **translator** (JS, in `editor.html`) command → Excalidraw API.
- [ ] `create_diagram` + `update_shapes` via the command schema. No vision yet.
- [ ] **Selection routing**: read `selectedElementIds` → resolve "this" → Controller-direct commands for simple edits.
- [ ] **Rolling local capture buffer** of `(snapshot, voice-fragment)` pairs (in-memory).

### Phase 3 — Markdown Embedding + Copy-on-Write
- [ ] Embed accepted-version SVG into the doc markdown (`<slug>.md`) on each commit.
- [ ] **Fork the document version (copy-on-write) before re-pointing links** when editing an opened existing version.
- [ ] `revert_diagram` (undo over committed versions; re-points link; reloads `.excalidraw`).
- [ ] Draft recovery from `.session/` on reload.

### Phase 4 — The Vision Loop (only after local editing is solid)
- [ ] `generate_snapshot` (client export → temporary `snapshot_ref`, request id/timeout).
- [ ] Opt-in gate before `interpret_sketch`.
- [ ] `interpret_sketch`: send **scribble PNG + voice/intent transcript + selected id + scene JSON** to `vision_model_fast` (escalate to `vision_model_quality`); receive **commands**; validate; apply; commit `v<k>`.
- [ ] **Web-image nodes**: `search_image` + `set_node_image` (reuse DuckDuckGo image-search infra) backing `set_image`/`add_image`; add as Excalidraw `image` elements, copy into `images/`.

---

## Acceptance Criteria (pass/fail per milestone)

- [ ] **Editor loads** (`@excalidraw/excalidraw` in the iframe) and accepts elements via `postMessage`.
- [ ] **Snapshot export is non-blank** (PNG + SVG) for a populated canvas.
- [ ] **SVG link inserted without deleting markdown** — embedding preserves surrounding doc content.
- [ ] **Opened-existing doc forks correctly** — first diagram edit on an opened version creates `version_N+1`; original untouched.
- [ ] **Revert re-points markdown** — restores previous SVG and reloads the matching `.excalidraw`.
- [ ] **Invalid command rejected** — malformed/unknown command refused and re-prompted; canvas unchanged.
- [ ] **Stale update rejected** — `update_shapes` with a mismatched `expected_version` returns `STALE_VERSION`.
- [ ] **Browser refresh restores draft** — uncommitted work survives a reload via `.session/`.
- [ ] **Shell/voice state survives focus exit** — leaving `diagram_focus` returns to a working session.
- [ ] **Legacy Mermaid placeholder** — a doc with old ```mermaid blocks shows the placeholder, not a blank.
- [ ] **Arrow binding** — a `connect` arrow stays attached when its nodes move.
- [ ] **No ghost scribbles** — after a cleanup pass, exactly the input strokes the model reported as replaced are removed; nothing else.

---

## Open Issues (test before building Phases 2–4)

- [ ] **🔬 TODO / SPIKE — Vision reliability (highest-risk assumption).** Assumes the Vision LLM reliably turns *scribbles + speech* into *correct, well-laid-out* diagram **commands**. Unproven — **test before building.**
  - **Harness:** `spikes/vision_spike.py` (throwaway). Feeds a hand-drawn PNG + spoken-intent sentence to a model and validates the returned `DiagramCommand[]` (schema + faithfulness + latency + tokens). *Library-agnostic — unaffected by the tldraw→Excalidraw switch; only the downstream translator target changed.*
  - **Cloud bake-off via OpenRouter:** one `OPENROUTER_API_KEY` → test the field by slug (`--provider openrouter --model <slug>`): `google/gemini-2.5-pro`, `anthropic/claude-sonnet-4.x`, `qwen/qwen2.5-vl-72b-instruct`, `mistralai/pixtral-large`. Plus direct `openai`/`gemini`/`anthropic` and `--provider all`.
  - **Local 4-bit (on-device):** `--provider local` (Ollama/MLX). **Hardware finding (8 GB MacBook Air):** Qwen2.5-VL **7B** 4-bit OOMs under MLX (~5.3 GB weights vs ~5.4 GB GPU budget); testing **3B 4-bit** → local is realistically a *helper/offline* tier, cloud (Gemini Flash) the primary generator.
  - **Follow-up:** tighten the spike's `SYSTEM` shape vocabulary to **Excalidraw-native shapes** (drop hexagon/cylinder/cloud) so the bake-off reflects what can actually be drawn.
  - **Results: TBD — update after testing.**
- [ ] **Shape-ceiling policy:** for shapes Excalidraw lacks (hexagon/cylinder/cloud/star/triangle), decide per shape: approximate, use a web-image icon, or reject.
- [ ] **Excalidraw API pinning** (Phase 2a): confirm `updateScene`/`exportToSvg`/`exportToBlob`/`serializeAsJSON`/`customData`/binding names against the pinned version.
- [ ] **Vision→commands ID reconciliation:** confirm the model reuses existing element ids (from scene JSON) and that replace-scribble semantics behave as intended.
- [ ] **Layout ownership:** keep-sketch vs. auto-layout (dagre/elk) vs. model-coordinate hints.
- [ ] **Web image quality/reliability:** DuckDuckGo image search rate-limits and returns mixed-license results; confirm icon quality or add a curated icon set fallback.
- [ ] **Round-trip UX latency:** measure export → vision → apply wall-clock.

### Vision spike results (fill in after testing via `spikes/vision_spike.py`)

| Model (provider) | Valid JSON? | Faithful to sketch? | Latency | Cost / notes |
| :--- | :--- | :--- | :--- | :--- |
| gpt-4o (openai) | TBD | TBD | TBD | TBD |
| gemini-2.0-flash (openrouter/gemini) | TBD | TBD | TBD | TBD |
| gemini-2.5-pro (openrouter) | TBD | TBD | TBD | TBD |
| claude-sonnet (anthropic/openrouter) | TBD | TBD | TBD | TBD |
| qwen2.5-vl-72b (openrouter) | TBD | TBD | TBD | TBD |
| qwen2.5vl:3b 4-bit (local) | TBD | TBD | TBD | on-device, 8 GB Air |

→ Pick `vision_model_fast` / `vision_model_quality` from these; decide if local 3B is usable.

---

## Summary

| Need | Solution | Rationale |
| :--- | :--- | :--- |
| Open licensing | **Excalidraw (MIT)** | Free to embed/modify/ship; no watermark or license key (vs. tldraw's proprietary SDK). |
| Safe AI edits | **App-owned command language** | Validated commands → Excalidraw API; version-stable, rollback-safe, library-swappable. |
| Multimodal | **Dual LLM (Controller + Vision)** | Separates logic from spatial reasoning; single writer. |
| Low cost | **Intent-driven, opt-in snapshots** | Vision only on demand; cheap direct commands for simple edits. |
| No backend renderer | **Client-side export → POST** | Deletes the headless-Chromium + file-watcher dependency. |
| Undo + history | **Per-iteration versions; snapshots ≠ versions** | Only accepted edits advance `v<k>`; copy-on-write on commit. |
| Portability | **SVG embedding** (= the vision snapshot) | Docs viewable anywhere; model sees what the doc shows. |
| Resilience | **`.session/` draft autosave** | Mobile reload / dropped session recovery without noisy versions. |
