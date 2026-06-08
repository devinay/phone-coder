# Claude Review Log: Document / Diagram Versioning Fix

## Context

User-reported bug:

- In document mode, entering image processing mode or diagram focus mode and then exiting caused only the diagram to remain in the markdown document; previous text was lost.
- Desired behavior: diagram or picture edits should be placed into the existing markdown document without deleting previous text.
- Opening an existing `version_0` and making edits should create `version_1`; all updated document data and assets should live there.
- If there are no edits, exit without creating or saving a new version.
- Edits include markdown changes, Mermaid diagram changes, and image-in-diagram changes.

## Files Changed

- `server/bot.py`
- `server/doc_state.py`
- `server/doc_storage.py`
- `server/tests/test_phase1_storage.py`
- `server/tests/test_phase2_diagrams.py`

## Code Changes

### `server/bot.py`

- Removed dead local `replacer()` function from `_update_diagram_in_doc`.
- Added module-level `_ensure_writable_version(session)`:
  - If a session opened an existing version and has not forked yet, it calls `fork_version(base)`.
  - Updates `session.version_info`, `session.version`, and `session.forked`.
  - Keeps newly-created projects writing directly to their own `version_0`.
- Added module-level `_mark_doc_session_edited(session)`.
- Ensured opened docs are tracked with `opened_existing=(action == "open")` when entering doc mode.
- Ensured document writes merge into `"Main Content"` when no section is provided, rather than replacing the whole markdown file.
- Ensured `insert_diagram` and `update_diagram` call `_ensure_writable_version()` before writing.
- Ensured `write_to_doc`, `insert_diagram`, and `update_diagram` mark the session edited via `_mark_doc_session_edited()`.
- Fixed image-in-diagram write paths:
  - `select_image()` forks before writing image assets and diagram URLs.
  - `select_image()` now errors if the target diagram block is missing instead of silently proceeding.
  - `select_image()` marks the doc session edited after persisting the updated markdown.
  - `resize_image()` now validates `version_info` and selected image presence.
  - `resize_image()` errors if the target diagram block is missing.
  - `resize_image()` marks the doc session edited after persisting the updated markdown.
- Updated controller prompt text so `write_to_doc` is described as preserving title, existing sections, and diagrams.

Claude follow-up fixes:

- `select_image()` now validates the target diagram block and Mermaid node before forking or copying an image, preventing a failed image embed from creating an orphan version.
- `exit_doc_mode()` now strips prior generated Summary/Transcript output before writing the newly generated Summary and transcript, avoiding duplicate generated sections on re-save.
- Added `_strip_generated_sections()` for that cleanup.

### `server/doc_state.py`

- Added `opened_existing` and `forked` session flags.
- `enter_doc_mode()` accepts `opened_existing`.
- `complete_save()` clears the copy-on-write flags.

### `server/doc_storage.py`

- Added `fork_version()` and exported it.
- `fork_version()` copies markdown, transcript, speakers, and asset directories into the next version and updates `project.json`.

## Tests Added

### `server/tests/test_phase1_storage.py`

Added `test_fork_version_copies_document_assets_and_updates_project_metadata`:

- Creates a project with `version_0`.
- Writes markdown containing text, a Mermaid diagram block, and an image URL pointing at `/version/0/images/...`.
- Writes transcript, speaker map, and an image asset.
- Calls `fork_version(v0)`.
- Asserts:
  - `version_1` is created with `derived_from == 0`.
  - `version_0` document is unchanged.
  - `version_1` preserves existing text and diagram block.
  - Embedded image URL is rewritten to `/version/1/images/...`.
  - Image asset, transcript, and speakers are copied.
  - `project.json` has `current_version == 1` and `versions == [0, 1]`.

### `server/tests/test_phase2_diagrams.py`

Added `test_update_embedded_image_preserves_surrounding_markdown`:

- Simulates embedding an image into a Mermaid node.
- Updates only the diagram block.
- Asserts intro and conclusion markdown remain present.

Added `test_ensure_writable_version_forks_opened_existing_once`:

- Creates a project with `version_0`.
- Builds an opened-existing session.
- Calls `_ensure_writable_version(session)`.
- Asserts:
  - `version_1` is created.
  - session now points at `version_1`.
  - `version_0` remains unchanged.
  - `project.json` current version is updated.
  - a second call does not create `version_2`.

Added `test_mark_doc_session_edited_records_diagram_edits`:

- Confirms the shared edit marker records persisted changes.

Added `test_strip_generated_sections_removes_prior_summary_and_transcript_once`:

- Confirms generated Summary and trailing Transcript are removed before re-save.
- Confirms user-authored body content remains.

## Validation Commands

Run from `server/`:

```bash
uv run pytest tests/test_phase1_storage.py tests/test_phase2_diagrams.py -q
```

Observed result:

```text
52 passed, 1 warning
```

Run from `server/`:

```bash
uv run pytest -q
```

Observed result:

```text
94 passed, 1 warning
```

Run from `server/`:

```bash
uv run ruff check bot.py tests/test_phase1_storage.py tests/test_phase2_diagrams.py
```

Observed result:

```text
All checks passed!
```

Note: running `uv run ruff check .` still reports pre-existing import ordering issues in unrelated files:

- `agent_router.py`
- `diagram_focus.py`
- `tests/test_phase1_state.py`
- `tests/test_poc4_atomic_write.py`

Those were not modified for this targeted fix.

## Review Focus For Claude

Please verify:

- No path still writes a new diagram/image source as the entire markdown document.
- Opened-existing sessions always fork before first persisted edit.
- Image selection and resizing now count as edits and land in the writable version.
- `version_0` remains unchanged after editing an opened existing document.
- Browser `doc-content-updated` payloads match the file content written to the selected version.
- No accidental broad refactor or unrelated behavior changes were introduced.

## Residual Risk

- The live voice tool handlers are nested inside `run_bot`, so direct unit testing of `select_image()` and `resize_image()` still requires heavier harnessing or more refactoring.
- The added tests cover the shared helpers and persistence behavior, but not a full end-to-end browser/voice session.
