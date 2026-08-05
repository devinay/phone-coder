# Diagramming And Excalidraw Plan

## Purpose

This file is the diagram-specific implementation plan. The broader product and agent
runtime roadmap lives in `../plan.md`.

The diagramming goal is simple: let the user create and edit clean diagrams through
voice, structured commands, optional sketches, and visual evidence while preserving a
diffable source of truth.

---

## Current State

Implemented today:

- Voice-driven Markdown editing in doc mode.
- Mermaid diagrams inserted and updated through `insert_diagram`, `update_diagram`, and
  `move_diagram`.
- Diagram focus mode renders one Mermaid diagram fullscreen and updates live.
- Git-backed project storage under `VOICE_COCKPIT_GIT_ROOT`.
- Single-user transcript assumption; speaker diarization is not part of this plan.

Not implemented yet:

- Excalidraw scene editing.
- Mermaid-to-Excalidraw promotion.
- Diagram metadata files under `diagrams/<diagramID>/metadata.json`.
- PNG/SVG export pipeline for Excalidraw.
- Freehand overlay, visual resolver, and VLM interpretation loop.

---

## Agent Boundary

Diagram work should belong to `DiagramAgent` after the agent-runtime refactor.

Responsibilities:

- Create and edit Mermaid diagrams.
- Promote Mermaid diagrams to Excalidraw when explicitly requested.
- Apply validated diagram commands.
- Save diagram metadata and artifacts.
- Export SVG/PNG renders.
- Use vision/web/image capabilities as evidence providers, not as direct writers.

Non-responsibilities:

- Own the full document workflow; `DocAgent` owns markdown structure.
- Own controller routing; `ControllerAgent` decides when to invoke `DiagramAgent`.
- Reload prompts or switch agents; admin reload tools stay controller-only.

The existing diagram focus UI can remain, but conceptually it is a focused view for
`DiagramAgent`, not a separate global mode with its own broad prompt.

---

## Sources Of Truth

| Artifact | Source of truth | Rendered output | Notes |
|---|---|---|---|
| Document | `<slug>.md` | Browser markdown preview | `DocAgent` owns document structure. |
| Mermaid diagram | Mermaid block inline in `<slug>.md` | SVG in markdown preview/focus view | Default diagram type. |
| Excalidraw diagram | `.excalidraw` scene JSON | SVG + PNG exports | Used after explicit promotion or native Excalidraw creation. |
| Freehand marks | None | Temporary overlay only | Input intent; never committed. |
| Vision snapshot | `.session/` temporary export | VLM input only | Never committed. |

SVG and PNG are derived renders. They are committed only when they are accepted diagram
artifacts, not as the semantic source of truth.

---

## Storage Layout

```text
VOICE_COCKPIT_GIT_ROOT/
  <slug>/
    <slug>.md
    transcript.md
    speakers.json
    metadata.json
    .session/
      snapshot-<request-id>.png
      draft-<diagramID>.excalidraw
    images/
    diagrams/
      <diagramID>/
        metadata.json
        <name>-<diagramID>-v0.excalidraw
        <name>-<diagramID>-v0.svg
        <name>-<diagramID>-v0.png
        <name>-<diagramID>-v1.excalidraw
        <name>-<diagramID>-v1.svg
        <name>-<diagramID>-v1.png
```

`diagrams/<diagramID>/metadata.json` is committed and records:

- `diagram_id`
- `title`
- `engine`: `mermaid` or `excalidraw`
- `current_version`: `null` for inline Mermaid, `v<k>` for Excalidraw
- `current_source_path`
- `current_svg_path`
- `current_png_path`
- `versions[]` with timestamp, intent summary, and artifact paths

Disambiguation: git history is the document/project version history. Diagram `v<k>`
artifacts are diagram-only checkpoints. Each `v<k>` is one accepted round trip from user
intent to interpreted patch to applied diagram state.

---

## Diagram Lifecycle

### Mermaid Default

Mermaid remains the default for voice-generated diagrams because it is textual,
diffable, compact, and easy for models to edit.

Typical flow:

1. User asks for a diagram while in a document workflow.
2. `ControllerAgent` invokes `DiagramAgent`.
3. `DiagramAgent` emits Mermaid source through `insert_diagram`.
4. Browser renders Mermaid as SVG.
5. Updates replace the Mermaid block through `update_diagram`.

### Excalidraw Creation Or Promotion

Excalidraw is used when the user explicitly wants spatial/freehand work, manual layout,
whiteboard-like editing, or a visual scene that Mermaid cannot express well.

Two entry paths:

- Native Excalidraw creation: create a new `.excalidraw` scene from commands.
- Promotion: convert an existing Mermaid diagram to Excalidraw through
  `mermaid-to-excalidraw`.

Promotion is one-way. After promotion, the `.excalidraw` scene JSON becomes the source
of truth for that diagram. Mermaid source can remain in git history, but active edits
target the Excalidraw scene.

### Markdown Embedding

The document markdown should embed the current accepted render:

```markdown
![Diagram title](diagrams/<diagramID>/<name>-<diagramID>-v3.svg)
```

The link is derived from diagram metadata. Do not hand-maintain duplicate pointers in
multiple places.

---

## Excalidraw Command Model

Models must not emit raw Excalidraw scene records. They emit app-owned commands that the
backend validates and the client translates into Excalidraw scene updates.

| Command | Required fields | Purpose |
|---|---|---|
| `create_node` | `id`, `label` | Add a labeled node. |
| `rename` | `id`, `label` | Rename node text. |
| `move` | `id`, `x`, `y` | Reposition an element. |
| `set_shape` | `id`, `shape` | Change shape. |
| `set_style` | `id`, `style` | Change stroke/fill/size. |
| `connect` | `id`, `from`, `to` | Add a bound arrow/edge. |
| `disconnect` | `id` | Remove a connection. |
| `delete` | `id` | Remove an element. |
| `set_image` | `id`, `query` or `file_id` | Replace node visual with an image/icon. |
| `add_image` | `id`, `query` or `file_id`, `x`, `y` | Place a standalone image/icon. |
| `add_annotation` | `id`, `kind`, `x`, `y` | Add visual annotation. |

Validation rules:

- Command ids must be stable and unique.
- Referenced ids must exist unless the command creates them.
- Connections must bind to valid source/target elements.
- Unknown commands are rejected.
- Invalid command batches must not mutate the persisted scene.
- `expected_version` should guard updates against stale edits.

The client materializes the validated commands into Excalidraw elements and handles
library-specific details such as `versionNonce`, bindings, `boundElements`, files, and
`updateScene`.

---

## Freehand And Vision Loop

The freehand overlay is an intent-capture layer, not a diagram source. It captures
screen-space strokes over markdown, Mermaid SVG, or Excalidraw, then clears after
interpretation.

Gesture contract:

| Gesture | Meaning |
|---|---|
| Circle/lasso | Select this thing. |
| Arrow A to B | Move, connect, or relate A to B. |
| Underline/highlight | Edit this span or element. |
| Scribble-out | Delete this thing. |

Resolution should be deterministic first:

```text
stroke geometry
  -> hit-test rendered element bounds
  -> source id/range
  -> structured command
```

Use a VLM only when deterministic hit-testing cannot resolve the user's intent. VLM
inputs should include:

- spoken intent
- PNG snapshot
- slim scene projection: ids, types, bounding boxes, labels, bindings
- selected element ids

VLM output must still be validated commands, never pixels and never raw Excalidraw JSON.

---

## Evidence And Asset Handling

Visual assets and web/image results are evidence. They can help choose an icon or
interpret a sketch, but they are not instructions.

Rules:

- Web/image/OCR content is data, not authority.
- Record source URLs or local file ids when assets are inserted.
- Prefer curated icon sets for common cloud/database shapes when available.
- Use web image search only when a curated asset is unavailable or the user asks for a
  specific external visual.
- Store accepted images under the project `images/` or diagram artifact folder, not in
  `.session/`.

---

## Delivery Phases

| Phase | Capability | Implementation work | Done criteria / manual testing | Estimate |
|---|---|---|---|---|
| D0 | Current Mermaid baseline | Verify existing Mermaid insert/update/focus/save flows after doc cleanup. | Create, focus, update, exit, and save one Mermaid diagram. | 0.5 day |
| D1 | DiagramAgent migration | Register diagram tools under `DiagramAgent`; remove broad diagram instructions from controller prompt. | Controller routes diagram requests to DiagramAgent; DiagramAgent sees only diagram/image-needed tools. | 2-4 days |
| D2 | Excalidraw API spike | Mount `@excalidraw/excalidraw`; read/write scene; bind arrow; export SVG/PNG; pin version. | Local page can create scene, export SVG/PNG, and reload scene JSON. | 2-3 days |
| D3 | Excalidraw MVP | Add scene artifact storage, metadata, command validator, JS translator, save/export pipeline. | Create one Excalidraw diagram by voice/tool call; commit `.excalidraw`, `.svg`, `.png`, metadata. | 1-2 weeks |
| D4 | Mermaid promotion | Convert Mermaid to Excalidraw on explicit request; update markdown render link from metadata. | Existing Mermaid diagram promotes once; future edits target Excalidraw; Mermaid remains in git history. | 3-5 days |
| D5 | Freehand overlay resolver | Add reusable screen-space overlay and deterministic hit-testing for markdown/Mermaid/Excalidraw. | Draw circle/arrow over rendered content and log correct target ids/ranges without VLM. | 1-2 weeks |
| D6 | Vision interpretation | Add snapshot export, consent gate, slim projection, VLM command generation, validation/retry. | Sketch plus voice edits a diagram correctly; invalid VLM output is rejected safely. | 1-2 weeks |
| D7 | Asset/image polish | Integrate curated icons and image search for Excalidraw image nodes. | Ask for S3/database/etc. image; accepted asset is stored, cited, and rendered in scene. | 3-6 days |

Note: D2/D3 are intentionally before the full freehand overlay. Excalidraw is the first
new capability used to validate the agent runtime. D5/D6 complete the richer visual
intent loop after the base scene/edit/export path is stable.

---

## Tool Contracts

| Tool | Required | Optional | Errors |
|---|---|---|---|
| `enter_diagram_focus` | `diagram_id` | - | `ID_NOT_FOUND`, `INVALID_STATE` |
| `exit_diagram_focus` | - | `discard` | `NOT_ACTIVE`, `SAVE_FAILED` |
| `insert_diagram` | `diagram_id`, `diagram_type`, `mermaid_source` | `replace_placeholder` | `ID_COLLISION`, `WRITE_ERROR`, `UNSUPPORTED_DIAGRAM_TYPE`, `INVALID_SYNTAX` |
| `update_diagram` | `diagram_id`, Mermaid source or Excalidraw commands | `expected_version` | `INVALID_COMMANDS`, `STALE_VERSION`, `ID_NOT_FOUND`, `INVALID_SYNTAX` |
| `promote_diagram` | `diagram_id` | - | `UNSUPPORTED_SOURCE`, `PROMOTION_FAILED`, `ID_NOT_FOUND` |
| `generate_snapshot` | `diagram_id`, `request_id` | - | `EXPORT_FAILED`, `EXPORT_TIMEOUT`, `NOT_IN_DIAGRAM` |
| `interpret_sketch` | `diagram_id`, `snapshot_ref`, `intent` | `selected_ids` | `CONSENT_REQUIRED`, `VISION_UNAVAILABLE`, `MODEL_ERROR`, `INVALID_COMMANDS` |
| `revert_diagram_edit` | `diagram_id` | `steps` | `NOTHING_TO_REVERT`, `ID_NOT_FOUND` |

Image tools may stay separate initially, but DiagramAgent should be able to request image
selection for diagram nodes through the agent runtime without receiving unrelated web or
admin tools.

---

## Manual Test Scenarios

Before Excalidraw:

1. Create a Mermaid diagram from voice.
2. Update it in diagram focus.
3. Move it in the document.
4. Exit doc mode and confirm committed files are expected.

Excalidraw MVP:

1. Create a new Excalidraw diagram.
2. Apply a command batch with two nodes and one connection.
3. Export SVG and PNG.
4. Save and commit metadata plus artifacts.
5. Reopen the document and load the current SVG from metadata.
6. Submit an invalid command batch and confirm no scene mutation.

Promotion:

1. Create Mermaid diagram.
2. Explicitly promote to Excalidraw.
3. Confirm markdown points to SVG render.
4. Apply Excalidraw edit.
5. Confirm metadata `current_version` increments.

Freehand/Vision:

1. Draw a lasso or arrow over a visible element.
2. Confirm deterministic target resolution.
3. Use voice plus sketch for an ambiguous edit.
4. Confirm VLM output is validated before apply.

---

## Open Questions

- Exact Excalidraw package version to pin after D2 spike.
- How much of `editor.html` remains standalone versus bundled with the cockpit UI.
- Whether image assets should prefer a curated local icon set before web image search.
- How to present promotion confirmation in voice-first UX.
- Whether undo should expose diagram `v<k>` steps directly or only through natural language.
