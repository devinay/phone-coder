"""Documentation editing and diagram insertion tools."""

import os
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

# Single source of truth for markdown/Mermaid helpers lives in helpers.py.
from helpers import (
    MERMAID_SUPPORTED_TYPES,
    MERMAID_UNSUPPORTED_TYPES,
    _extract_diagram_source,
    _insert_diagram_in_doc,
    _replace_section,
    _strip_generated_sections,
    _update_diagram_in_doc,
    _validate_mermaid_source,
)

if TYPE_CHECKING:
    from diagram_focus import DiagramFocusStateMachine
    from doc_state import DocStateMachine


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
        # Same trap that silently broke every session summary: newer models
        # reject `max_tokens` and want `max_completion_tokens`. This path pins
        # gpt-4o-mini, which still accepts the old name, so it works today and
        # would break the moment the model is changed. Routed through the same
        # helper so it cannot.
        from memory import create_with_token_limit

        resp = await create_with_token_limit(
            client,
            model="gpt-4o-mini",
            limit=200,
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
    from pipecat.frames.frames import OutputTransportMessageUrgentFrame
    from pipecat.processors.frameworks.rtvi.models import ServerMessage

    from doc_state import StateMachineError
    from git_storage import (
        atomic_write,
        create_project,
        docs_root,
        list_projects,
        load_document,
        load_project,
        save_document,
    )

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
        save_report = ""
        had_edits = session.has_edits

        if state == "shell":
            await params.result_callback("Documentation mode is not active.")
            return

        # If we're in (or stuck in) diagram focus, tear it down first so the inner
        # diagram state machine never outlives the doc session. Bring doc_sm back to
        # doc_mode when possible so the normal save path below runs.
        if diagram_focus_sm.is_active:
            try:
                diagram_focus_sm.exit()
            except Exception as e:
                logger.warning(f"[DOC] diagram_focus_sm.exit() during doc exit: {e}")
        if state == "diagram_focus":
            try:
                doc_sm.exit_diagram_focus()  # diagram_focus → doc_mode
                state = doc_sm.session.state.value
            except StateMachineError as e:
                logger.warning(f"[DOC] could not exit diagram focus cleanly: {e}")

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
                save_result = save_document(
                    doc_info,
                    final_doc,
                    transcript_content,
                    speaker_data,
                    "Save documentation session",
                )

                # Push updated content to browser before overlay closes
                content_msg = ServerMessage(data={"type": "doc-content-updated", "content": final_doc})
                await task.queue_frames([OutputTransportMessageUrgentFrame(message=content_msg.model_dump())])

                files = "\n".join(f"- {path}" for path in save_result.committed_files)
                sha_line = save_result.commit_sha[:12] if save_result.commit_sha else "no new commit"
                push_line = (
                    f"Push: {save_result.push_message}\n"
                    f"Files pushed:\n{files}"
                    if save_result.push_success
                    else f"Push warning: {save_result.warning_message}"
                )
                save_report = f"\nCommit: {sha_line}\nFiles committed:\n{files}\n{push_line}"

                if not save_result.push_success:
                    logger.warning(f"[DOC] {save_result.warning_message}")

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
        elif had_edits:
            action_taken = "saved"
        else:
            action_taken = "closed without changes"
        await params.result_callback(
            f"Documentation mode closed. Session {action_taken}. Terminal is restored."
            f"{save_report}"
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
                + ("Previous valid source preserved." if prev else "No previous valid source.")
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
                await params.result_callback(
                    f"INVALID_SYNTAX: {err}. "
                    "Previous valid source preserved in document."
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
