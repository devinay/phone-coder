"""Documentation editing and diagram insertion tools."""

import os
import re
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

if TYPE_CHECKING:
    from diagram_focus import DiagramFocusStateMachine
    from doc_state import DocStateMachine, StateMachineError


# Diagram types the Mermaid CDN version supports reliably in production
MERMAID_SUPPORTED_TYPES = {
    "flowchart", "graph", "sequenceDiagram", "classDiagram",
    "stateDiagram", "stateDiagram-v2", "erDiagram", "gantt",
    "pie", "gitGraph", "journey", "mindmap", "timeline",
    "block-beta", "quadrantChart",
}

# Types that are beta/unstable — agent must re-prompt with a supported fallback
MERMAID_UNSUPPORTED_TYPES = {
    "xychart-beta", "sankey-beta", "C4Context", "C4Container", "C4Component",
}

_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_DIAGRAM_BLOCK_RE = re.compile(
    r'(<!-- diagram-id: (?P<id>[a-z0-9_-]+) -->\n```mermaid\n)(?P<src>.*?)```',
    re.DOTALL,
)
_DIAGRAM_PLACEHOLDER_RE = re.compile(r'<!-- diagram: (?P<desc>[^>]+?) -->')


def _find_section(lines: list[str], section: str) -> tuple[int | None, int]:
    """Find a markdown header (## .. ######) whose title equals `section`.

    Returns (start_index, level). start_index is None if not found.
    """
    target = section.strip()
    for i, l in enumerate(lines):
        m = _MD_HEADER_RE.match(l)
        if m and len(m.group(1)) >= 2 and m.group(2).strip() == target:
            return i, len(m.group(1))
    return None, 2


def _section_end(lines: list[str], start: int, level: int) -> int | None:
    """Index of the next header at the same or shallower level after `start`, or None."""
    for i in range(start + 1, len(lines)):
        m = _MD_HEADER_RE.match(lines[i])
        if m and len(m.group(1)) <= level:
            return i
    return None


def _replace_section(doc: str, section: str, new_content: str) -> str:
    """Replace the body under a markdown header named `section` (any level ##–######),
    or append a new ## section if none exists. Matching by name (not level) means an
    existing ### subsection is edited in place instead of spawning a duplicate ## header.
    """
    lines = doc.splitlines(keepends=True)
    start, level = _find_section(lines, section)
    if start is None:
        header = f"## {section}"
        sep = "\n\n" if doc and not doc.endswith("\n\n") else ""
        return doc + sep + header + "\n\n" + new_content.strip() + "\n"
    end = _section_end(lines, start, level)
    before = "".join(lines[: start + 1])
    after = "".join(lines[end:]) if end is not None else ""
    return before + "\n\n" + new_content.strip() + "\n\n" + after


def _strip_generated_sections(doc: str) -> str:
    """Remove a previously generated ``## Summary`` block and trailing Transcript
    ``<details>`` so re-saving an opened document replaces them instead of stacking
    duplicates. Content the user authored in between is left untouched.
    """
    # Trailing transcript collapsible (plus any --- separator that precedes it).
    doc = re.sub(
        r"\n*(?:---[ \t]*\n+)?<details>\s*<summary>Transcript</summary>.*?</details>\s*$",
        "\n",
        doc,
        flags=re.DOTALL,
    )
    # Leading "## Summary ... \n---\n" block (only the first occurrence).
    doc = re.sub(
        r"## Summary\n.*?\n---[ \t]*\n+",
        "",
        doc,
        count=1,
        flags=re.DOTALL,
    )
    return doc.rstrip() + "\n"


def _validate_mermaid_source(source: str, diagram_type: str) -> tuple[bool, str]:
    """Check that source starts with the declared diagram type keyword.

    Returns (is_valid, error_message).
    """
    stripped = source.strip()
    valid_starts = {diagram_type}
    if diagram_type == "flowchart":
        valid_starts.add("graph")
    for start in valid_starts:
        if stripped.lower().startswith(start.lower()):
            return True, ""
    first_line = stripped.splitlines()[0] if stripped else "(empty)"
    return False, (
        f"Source first line '{first_line}' does not match declared type '{diagram_type}'. "
        f"Mermaid source must begin with the diagram keyword."
    )


def _build_diagram_block(diagram_id: str, mermaid_source: str) -> str:
    return f"<!-- diagram-id: {diagram_id} -->\n```mermaid\n{mermaid_source.strip()}\n```"


def _insert_diagram_in_doc(
    doc: str, diagram_id: str, mermaid_source: str, replace_placeholder: str | None
) -> str:
    """Insert a new diagram block, optionally replacing a placeholder comment."""
    block = _build_diagram_block(diagram_id, mermaid_source)
    if replace_placeholder:
        # Try exact text match first, then partial match
        placeholder = f"<!-- diagram: {replace_placeholder} -->"
        if placeholder in doc:
            return doc.replace(placeholder, block, 1)
        # Partial match: find any placeholder whose description contains the text
        m = _DIAGRAM_PLACEHOLDER_RE.search(doc)
        if m:
            return doc[:m.start()] + block + doc[m.end():]
    # No placeholder: append before the last newline or at end
    sep = "\n\n" if doc and not doc.endswith("\n\n") else ""
    return doc + sep + block + "\n"


def _update_diagram_in_doc(doc: str, diagram_id: str, mermaid_source: str) -> tuple[str, bool]:
    """Replace the mermaid source inside an existing diagram block.

    Returns (new_doc, found).
    """
    block = _build_diagram_block(diagram_id, mermaid_source)

    new_doc, count = _DIAGRAM_BLOCK_RE.subn(
        lambda m: block if m.group("id") == diagram_id else m.group(0),
        doc,
    )
    return new_doc, count > 0


def _extract_diagram_source(doc: str, diagram_id: str) -> str | None:
    """Extract the mermaid source for a diagram block by ID. Returns None if not found."""
    for m in _DIAGRAM_BLOCK_RE.finditer(doc):
        if m.group("id") == diagram_id:
            return m.group("src").strip()
    return None




def _mark_doc_session_edited(session: Any) -> None:
    """Record that the active documentation session has persisted changes."""
    session.has_edits = True


async def _generate_doc_summary(doc_content: str, api_key: str) -> str:
    """Call OpenAI to produce a ≤5-line plain-text summary of the document."""
    if not api_key or not doc_content.strip():
        return ""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You summarise documents. Write a plain-text summary in 5 lines or fewer. "
                        "No bullet points, no markdown, no headers. Just concise prose."
                    ),
                },
                {"role": "user", "content": f"Summarise this document:\n\n{doc_content[:4000]}"},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"[DOC] Summary generation failed: {e}")
        return ""


def create_doc_tools(
    doc_sm: "DocStateMachine",
    diagram_focus_sm: "DiagramFocusStateMachine",
    task: Any,
    router: Any,
) -> dict[str, Any]:
    """Factory function to create documentation tools.

    Args:
        doc_sm: DocStateMachine instance
        diagram_focus_sm: DiagramFocusStateMachine instance
        task: PipelineTask for queueing frames
        router: AgentRouter instance

    Returns a dictionary of tool functions ready for registration.
    """
    from git_storage import atomic_write, create_project, docs_root, list_projects, load_project, load_document, save_document, save_diagram
    from pipecat.processors.frameworks.rtvi.models import ServerMessage
    from pipecat.frames.frames import OutputTransportMessageUrgentFrame
    from doc_state import StateMachineError

    async def list_doc_projects(params: FunctionCallParams):
        """List all existing documentation projects.

        Call this before asking the user which project to open, so you can
        present them with the available options instead of asking for a slug blindly.
        """
        projects = list_projects()
        if not projects:
            await params.result_callback(
                f"NO_PROJECTS: No documentation projects found in {docs_root()}."
            )
            return
        lines = [
            f"- slug='{p['slug']}', name='{p['display_name']}'"
            for p in projects
        ]
        await params.result_callback("Available projects:\n" + "\n".join(lines))

    async def enter_doc_mode(
        params: FunctionCallParams,
        action: str,
        topic_name: str = "",
        project_slug: str = "",
    ):
        """Enter Documentation Mode to create or open a documentation project.

        Call this when the user wants to start or continue a documentation session.

        Args:
            action: "create" to start a new project, "open" to load an existing one.
            topic_name: Human-readable project name (used when action="create").
            project_slug: Existing project slug (used when action="open").
        """
        session = doc_sm.session

        # Idempotent: already in doc_mode
        if session.state.value == "doc_mode":
            logger.info("[DOC] enter_doc_mode: already active (no-op)")
            await params.result_callback(
                f"ALREADY_ACTIVE: Documentation mode is already open "
                f"(project: {session.project_slug})."
            )
            return

        try:
            if action == "create":
                if not topic_name:
                    await params.result_callback(
                        "ERROR: topic_name is required when action='create'."
                    )
                    return
                project_info, doc_info = create_project(topic_name)
                logger.info(
                    f"[DOC] Created project '{project_info.slug}' at {project_info.project_dir}"
                )
            elif action == "open":
                slug = project_slug or topic_name
                if not slug:
                    await params.result_callback(
                        "ERROR: project_slug is required when action='open'."
                    )
                    return
                project_info = load_project(slug)
                if project_info is None:
                    await params.result_callback(
                        f"PROJECT_NOT_FOUND: No project with slug '{slug}'."
                    )
                    return
                doc_info = load_document(project_info.project_dir)
                logger.info(
                    f"[DOC] Opened project '{project_info.slug}'"
                )
            else:
                await params.result_callback(
                    f"ERROR: Unknown action '{action}'. Use 'create' or 'open'."
                )
                return

            doc_sm.enter_doc_mode(
                project_slug=project_info.slug,
                version_info=doc_info,
                project_dir=project_info.project_dir,
                opened_existing=(action == "open"),
            )

            # Push browser event
            msg = ServerMessage(
                data={
                    "type": "doc-mode-entered",
                    "project_slug": project_info.slug,
                }
            )
            frames = [OutputTransportMessageUrgentFrame(message=msg.model_dump())]

            # For existing projects, push the current file content to the browser overlay
            if action == "open" and doc_info and doc_info.document_md.exists():
                existing_content = doc_info.document_md.read_text()
                if existing_content.strip():
                    content_msg = ServerMessage(
                        data={"type": "doc-content-updated", "content": existing_content}
                    )
                    frames.append(OutputTransportMessageUrgentFrame(message=content_msg.model_dump()))

            await task.queue_frames(frames)
            logger.info(f"[DOC STATE] shell → doc_mode (project={project_info.slug})")

            await params.result_callback(
                f"Documentation mode active. Project: '{project_info.display_name}'. "
                f"Files are at {doc_info.project_dir if doc_info else 'unknown'}."
            )

        except StateMachineError as e:
            logger.error(f"[DOC] State machine error in enter_doc_mode: {e}")
            await params.result_callback(f"{e.code}: {e.message}")
        except Exception as e:
            logger.error(f"[DOC] Unexpected error in enter_doc_mode: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def exit_doc_mode(params: FunctionCallParams, discard: bool = False):
        """Exit Documentation Mode, save the document, and restore the terminal.

        Args:
            discard: If True, exit without saving (discards the current session content).
        """
        session = doc_sm.session
        state = session.state.value

        if state == "shell":
            await params.result_callback("Documentation mode is not active.")
            return

        # Move into the saving state. If a previous exit attempt already left us in
        # 'saving' or 'error_recovery', resume the close instead of failing — exiting
        # must always succeed so the user is never stuck.
        if state == "doc_mode":
            try:
                doc_sm.exit_doc_mode()  # doc_mode → saving
            except StateMachineError as e:
                logger.error(f"[DOC] State machine error in exit_doc_mode: {e}")
                await params.result_callback(f"{e.code}: {e.message}")
                return
        else:
            logger.warning(f"[DOC] exit_doc_mode resuming from stuck state '{state}'")

        if not discard and session.has_edits and session.version_info and session.doc_writer:
            try:
                doc_info = session.version_info
                import json

                # Read the current document.md (written incrementally by write_to_doc).
                # Strip any prior generated Summary/Transcript so re-saving an opened
                # document replaces them instead of duplicating.
                current_doc = doc_info.document_md.read_text() if doc_info.document_md.exists() else ""
                current_doc = _strip_generated_sections(current_doc)

                # Generate a ≤5-line summary via LLM. A summary failure must never
                # abort the save — fall back to saving without one.
                try:
                    summary_text = await _generate_doc_summary(current_doc, os.getenv("OPENAI_API_KEY", ""))
                except Exception as e:
                    logger.warning(f"[DOC] Summary generation failed, saving without summary: {e}")
                    summary_text = ""

                # Build the final document:
                # 1. Summary section at top
                # 2. Existing document content (write_to_doc writes the body already)
                # 3. Transcript collapsible at the very end
                transcript_block = session.doc_writer.render_transcript_collapsible()

                if summary_text:
                    if "## Summary" in current_doc:
                        # Update the existing Summary section in place (no duplication on re-save).
                        final_doc = _replace_section(current_doc, "Summary", summary_text)
                    elif current_doc.startswith("#"):
                        # No Summary yet: insert one right after the # Title line.
                        title_end = current_doc.index("\n") + 1
                        final_doc = (
                            current_doc[:title_end]
                            + f"\n## Summary\n\n{summary_text}\n\n"
                            + current_doc[title_end:]
                        )
                    else:
                        final_doc = f"## Summary\n\n{summary_text}\n\n" + current_doc
                else:
                    final_doc = current_doc

                # Ensure transcript is always last
                final_doc = final_doc.rstrip() + "\n\n---\n\n" + transcript_block + "\n"

                # Save document and commit to git
                transcript_content = session.doc_writer.render_transcript_md()
                speaker_data = session.speaker_map
                save_document(doc_info, final_doc, transcript_content, speaker_data, "Save documentation session")

                # Push updated content to browser before overlay closes
                content_msg = ServerMessage(data={"type": "doc-content-updated", "content": final_doc})
                await task.queue_frames([OutputTransportMessageUrgentFrame(message=content_msg.model_dump())])

                logger.info(
                    f"[DOC] Saved with summary + transcript collapsible "
                    f"({session.doc_writer.utterance_count()} utterances) to {doc_info.document_md}"
                )
            except Exception as e:
                # Never wedge: force the session back to shell so the user can re-enter.
                logger.error(f"[DOC] Save failed, force-closing doc mode: {e}")
                doc_sm.recover_to_shell()
                await task.queue_frames([
                    OutputTransportMessageUrgentFrame(
                        message=ServerMessage(data={"type": "doc-mode-exited"}).model_dump()
                    )
                ])
                await params.result_callback(
                    f"SAVE_FAILED: {e}. Documentation mode was closed anyway — your last "
                    f"saved content is intact; re-open the project to continue."
                )
                return
        elif not discard and not session.has_edits:
            logger.info("[DOC] No edits — exiting without save")

        doc_sm.complete_save()

        # Push browser event
        msg = ServerMessage(data={"type": "doc-mode-exited"})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info("[DOC STATE] doc_mode → shell")

        # Verify terminal is still responsive
        router.capture_output(5)

        if discard:
            action_taken = "discarded"
        elif session.has_edits:
            action_taken = "saved"
        else:
            action_taken = "closed without changes"
        await params.result_callback(
            f"Documentation mode closed. Session {action_taken}. Terminal is restored."
        )

    async def read_doc(params: FunctionCallParams):
        """Read the current contents of document.md for the active documentation session.

        Call this before making any targeted edits so you can see existing headers and content.
        """
        session = doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        content = vi.document_md.read_text() if vi.document_md.exists() else ""
        await params.result_callback(content if content.strip() else "(Document is empty.)")

    async def write_to_doc(params: FunctionCallParams, content: str, section: str = ""):
        """Write agreed content to document.md for the active documentation session.

        Only call this after the user has confirmed the content and format.

        Args:
            content: The markdown content to write.
            section: If provided, replace only the content under this ## header.
                     If empty, content is written under the "Main Content" section,
                     leaving the title, other sections and diagrams untouched.
        """
        session = doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            # Always merge into a section so the title, diagrams and other sections
            # are never destroyed. An empty section targets "Main Content".
            new_doc = _replace_section(current, section or "Main Content", content)
            atomic_write(vi.document_md, new_doc)

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(session)
            logger.info(f"[DOC] write_to_doc section={section!r} ({len(new_doc)} chars)")
            await params.result_callback(
                "OK: Document updated. "
                "Now call read_doc to review the saved content, then say to the user: "
                "'This looks like a document based on the context of the conversation. "
                "Would you like to generate any associated drawings or diagrams?'"
            )
        except Exception as e:
            logger.error(f"[DOC] write_to_doc failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def edit_doc(params: FunctionCallParams, find: str, replace: str):
        """Surgically replace an exact span of text in document.md.

        Use this for any in-place change — fixing the title, correcting a word,
        rewording a sentence — instead of rewriting whole sections. The match
        must be unique: if it appears zero or multiple times, nothing is written.

        Args:
            find: The exact existing text to replace (include enough context to be unique).
            replace: The new text to put in its place.
        """
        session = doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return
        if not find:
            await params.result_callback("ERROR: 'find' must not be empty.")
            return
        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            count = current.count(find)
            if count == 0:
                await params.result_callback(
                    "NOT_FOUND: That exact text is not in the document. "
                    "Call read_doc to see the current content, then retry with text copied verbatim."
                )
                return
            if count > 1:
                await params.result_callback(
                    f"AMBIGUOUS: '{find[:40]}...' appears {count} times. "
                    "Include more surrounding text so the match is unique."
                )
                return
            new_doc = current.replace(find, replace, 1)
            atomic_write(vi.document_md, new_doc)

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(session)
            logger.info(f"[DOC] edit_doc ({len(find)} → {len(replace)} chars)")
            await params.result_callback("OK: Edit applied.")
        except Exception as e:
            logger.error(f"[DOC] edit_doc failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def insert_diagram(
        params: FunctionCallParams,
        diagram_id: str,
        diagram_type: str,
        mermaid_source: str,
        replace_placeholder: str = "",
    ):
        """Insert a new Mermaid diagram block into document.md.

        Call this when the user asks to draw or generate a diagram.
        Before calling, check MERMAID_SUPPORTED_TYPES — if the requested type is unsupported,
        tell the user and use 'flowchart' as the fallback type instead.

        Args:
            diagram_id: Unique slug for this diagram (e.g. 'auth-flow', 'class-diagram-1').
            diagram_type: Mermaid diagram keyword (e.g. 'sequenceDiagram', 'flowchart').
            mermaid_source: Complete Mermaid source starting with the diagram type keyword.
            replace_placeholder: If provided, replace the matching <!-- diagram: ... --> comment.
        """
        session = doc_sm.session
        if session.state.value not in ("doc_mode", "diagram_focus"):
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return

        # Reject unsupported types — caller must use a supported fallback
        if diagram_type in MERMAID_UNSUPPORTED_TYPES:
            fallback_list = ", ".join(sorted(MERMAID_SUPPORTED_TYPES - {"graph"}))
            await params.result_callback(
                f"UNSUPPORTED_DIAGRAM_TYPE: '{diagram_type}' is not supported "
                f"(beta/experimental). Use one of: {fallback_list}. "
                f"Re-prompt the user with the constraint and suggest 'flowchart' as default."
            )
            return

        if diagram_type not in MERMAID_SUPPORTED_TYPES:
            await params.result_callback(
                f"UNKNOWN_DIAGRAM_TYPE: '{diagram_type}' is not a known Mermaid type."
            )
            return

        ok, err = _validate_mermaid_source(mermaid_source, diagram_type)
        if not ok:
            prev = session.last_valid_diagrams.get(diagram_id)
            await params.result_callback(
                f"INVALID_SYNTAX: {err}. "
                + (f"Previous valid source preserved." if prev else "No previous valid source.")
            )
            return

        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            new_doc = _insert_diagram_in_doc(current, diagram_id, mermaid_source, replace_placeholder or None)
            atomic_write(vi.document_md, new_doc)
            session.last_valid_diagrams[diagram_id] = mermaid_source

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(session)
            logger.info(f"[DOC] insert_diagram id={diagram_id} type={diagram_type}")
            await params.result_callback(
                f"OK: Diagram '{diagram_id}' inserted. "
                f"Ask the user: 'Do you want to edit diagram \"{diagram_id}\" now?' "
                f"If yes, call enter_diagram_focus(diagram_id='{diagram_id}')."
            )
        except Exception as e:
            logger.error(f"[DOC] insert_diagram failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def update_diagram(
        params: FunctionCallParams,
        diagram_id: str,
        mermaid_source: str,
    ):
        """Replace the Mermaid source of an existing diagram block in document.md.

        Call this when the user asks to change or edit an existing diagram.
        If the new source is invalid, the previous valid source is preserved.

        Args:
            diagram_id: The ID of the diagram to update (must already exist in the document).
            mermaid_source: New complete Mermaid source starting with the diagram type keyword.
        """
        session = doc_sm.session
        if session.state.value not in ("doc_mode", "diagram_focus"):
            await params.result_callback("NOT_IN_DOC_MODE: No active documentation session.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return

        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            existing_src = _extract_diagram_source(current, diagram_id)
            if existing_src is None:
                await params.result_callback(
                    f"ID_NOT_FOUND: No diagram with id '{diagram_id}' in document.md."
                )
                return

            # Infer diagram type from existing source for validation
            first_word = mermaid_source.strip().split()[0] if mermaid_source.strip() else ""
            ok, err = _validate_mermaid_source(mermaid_source, first_word)
            if not ok or first_word not in MERMAID_SUPPORTED_TYPES:
                prev = session.last_valid_diagrams.get(diagram_id, existing_src)
                await params.result_callback(
                    f"INVALID_SYNTAX: {err}. "
                    f"Previous valid source preserved in document."
                )
                return

            new_doc, found = _update_diagram_in_doc(current, diagram_id, mermaid_source)
            if not found:
                await params.result_callback(f"ID_NOT_FOUND: Could not locate diagram '{diagram_id}'.")
                return

            atomic_write(vi.document_md, new_doc)
            session.last_valid_diagrams[diagram_id] = mermaid_source

            frames = []
            # Always update the doc content for when focus mode exits
            frames.append(OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
            ))
            # Also push a live re-render event if we're currently in focus mode
            if session.state.value == "diagram_focus":
                diagram_focus_sm.begin_edit()
                diagram_focus_sm.begin_save()
                diagram_focus_sm.complete_save(mermaid_source)
                frames.append(OutputTransportMessageUrgentFrame(
                    message=ServerMessage(data={
                        "type": "diagram-focus-updated",
                        "diagram_id": diagram_id,
                        "mermaid_source": mermaid_source,
                    }).model_dump()
                ))
            await task.queue_frames(frames)
            _mark_doc_session_edited(session)
            logger.info(f"[DOC] update_diagram id={diagram_id} ({len(mermaid_source)} chars)")
            await params.result_callback(f"OK: Diagram '{diagram_id}' updated.")
        except Exception as e:
            logger.error(f"[DOC] update_diagram failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    return {
        "list_doc_projects": list_doc_projects,
        "enter_doc_mode": enter_doc_mode,
        "exit_doc_mode": exit_doc_mode,
        "read_doc": read_doc,
        "write_to_doc": write_to_doc,
        "edit_doc": edit_doc,
        "insert_diagram": insert_diagram,
        "update_diagram": update_diagram,
    }
