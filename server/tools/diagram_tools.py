"""Diagram editing and focus mode tools."""

import re
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

if TYPE_CHECKING:
    from diagram_focus import DiagramFocusStateMachine
    from doc_state import DocStateMachine, StateMachineError


def _extract_diagram_source(doc: str, diagram_id: str) -> str | None:
    """Extract the mermaid source for a diagram block by ID. Returns None if not found."""
    _DIAGRAM_BLOCK_RE = re.compile(
        r'(<!-- diagram-id: (?P<id>[a-z0-9_-]+) -->\n```mermaid\n)(?P<src>.*?)```',
        re.DOTALL,
    )
    for m in _DIAGRAM_BLOCK_RE.finditer(doc):
        if m.group("id") == diagram_id:
            return m.group("src").strip()
    return None


def _move_diagram_in_doc(doc: str, diagram_id: str, target_section: str) -> tuple[str, str]:
    """Relocate the (single, marked) diagram block to the end of `target_section`'s body.

    Removes the block from its current position and re-inserts it — preserving the
    ``<!-- diagram-id -->`` marker — so the diagram never ends up duplicated.

    Returns (new_doc, status) where status is one of:
      "ok"           — moved successfully
      "no_diagram"   — no block with that id
      "no_section"   — target section header not found (doc unchanged)
    """
    _DIAGRAM_BLOCK_RE = re.compile(
        r'(<!-- diagram-id: (?P<id>[a-z0-9_-]+) -->\n```mermaid\n)(?P<src>.*?)```',
        re.DOTALL,
    )
    _MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

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

    match = next((m for m in _DIAGRAM_BLOCK_RE.finditer(doc) if m.group("id") == diagram_id), None)
    if match is None:
        return doc, "no_diagram"
    block = doc[match.start():match.end()].strip()

    # Remove the block from its current location, collapsing the surrounding blank lines.
    without = (doc[:match.start()].rstrip() + "\n\n" + doc[match.end():].lstrip()).strip() + "\n"

    lines = without.splitlines(keepends=True)
    start, level = _find_section(lines, target_section)
    if start is None:
        return doc, "no_section"  # leave the original untouched

    end = _section_end(lines, start, level)
    end_idx = end if end is not None else len(lines)
    before = "".join(lines[:end_idx]).rstrip()
    after = "".join(lines[end_idx:]).lstrip()
    new_doc = before + "\n\n" + block + "\n\n" + after
    return new_doc.rstrip() + "\n", "ok"


def create_diagram_tools(
    doc_sm: "DocStateMachine",
    diagram_focus_sm: "DiagramFocusStateMachine",
    task: Any,
    context: Any = None,
) -> dict[str, Any]:
    """Factory function to create diagram editing and focus mode tools.

    Args:
        doc_sm: DocStateMachine instance
        diagram_focus_sm: DiagramFocusStateMachine instance
        task: PipelineTask for queueing frames
        context: LLMContext for adding system notes (optional)

    Returns a dictionary of tool functions ready for registration.
    """
    from git_storage import atomic_write
    from pipecat.processors.frameworks.rtvi.models import ServerMessage
    from pipecat.frames.frames import OutputTransportMessageUrgentFrame
    from .doc_tools import (
        _extract_diagram_source as get_diagram_source,
        _update_diagram_in_doc,
        _mark_doc_session_edited,
    )

    async def move_diagram(params: FunctionCallParams, diagram_id: str, target_section: str):
        """Move an existing diagram under a different section, without duplicating it.

        Use this when the user asks to move/relocate a diagram (e.g. 'put the diagram
        under the Fuel Considerations section'). Do NOT use write_to_doc to relocate a
        diagram — that leaves the original behind. The diagram keeps its id.

        Args:
            diagram_id: The id of the diagram to move (must already exist in the document).
            target_section: The exact header text to move it under (any level, e.g.
                'Fuel Considerations for Light Ships'). Call read_doc first to get it right.
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
            new_doc, status = _move_diagram_in_doc(current, diagram_id, target_section)
            if status == "no_diagram":
                await params.result_callback(
                    f"ID_NOT_FOUND: No diagram with id '{diagram_id}' in the document."
                )
                return
            if status == "no_section":
                await params.result_callback(
                    f"SECTION_NOT_FOUND: No section titled '{target_section}'. "
                    "Call read_doc to see the exact header text, then retry."
                )
                return

            atomic_write(vi.document_md, new_doc)

            msg = ServerMessage(data={"type": "doc-content-updated", "content": new_doc})
            await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
            _mark_doc_session_edited(session)
            logger.info(f"[DOC] move_diagram id={diagram_id} → section '{target_section}'")
            await params.result_callback(
                f"OK: Diagram '{diagram_id}' moved under '{target_section}'."
            )
        except Exception as e:
            logger.error(f"[DOC] move_diagram failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    async def enter_diagram_focus(params: FunctionCallParams, diagram_id: str):
        """Enter Diagram Focus Mode for a specific diagram.

        Hides all other UI and shows only the diagram fullscreen.
        In Focus Mode the controller only handles diagram edits — no terminal commands.
        Call this when the user asks to edit or view a specific diagram.

        Args:
            diagram_id: The diagram to focus on (must exist in document.md).
        """
        session = doc_sm.session
        if session.state.value != "doc_mode":
            await params.result_callback(
                f"INVALID_STATE: enter_diagram_focus requires doc_mode "
                f"(current: {session.state.value})."
            )
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info in current session.")
            return

        current = vi.document_md.read_text() if vi.document_md.exists() else ""
        src = get_diagram_source(current, diagram_id)
        if src is None:
            await params.result_callback(
                f"ID_NOT_FOUND: No diagram with id '{diagram_id}' in document.md."
            )
            return

        try:
            from doc_state import StateMachineError
            doc_sm.enter_diagram_focus(diagram_id)
            diagram_focus_sm.enter(diagram_id, src)
        except (StateMachineError, Exception) as e:
            await params.result_callback(f"ERROR: {e}")
            return

        # Inject scoped system prompt — narrows the controller to diagram-only commands
        if context:
            context.add_message({
                "role": "user",
                "content": (
                    "[SYSTEM NOTE — DIAGRAM FOCUS MODE ACTIVE]\n"
                    f"You are now in Diagram Focus Mode for diagram '{diagram_id}'.\n"
                    "RULES while in this mode:\n"
                    "1. Only respond to diagram-related requests (describe changes, update source, exit).\n"
                    "2. If the user asks to do something unrelated (run a command, write to doc, etc.), "
                    "politely say you can only handle diagram edits right now and ask them to exit first.\n"
                    "3. When the user describes a change, rewrite the Mermaid source and call update_diagram.\n"
                    "4. When the user says 'exit diagram mode', 'done', or 'save and exit', call exit_diagram_focus().\n"
                    "5. Keep replies short — the user is looking at the diagram, not reading text."
                ),
            })

        msg = ServerMessage(data={
            "type": "diagram-focus-entered",
            "diagram_id": diagram_id,
            "mermaid_source": src,
        })
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[DIAGRAM FOCUS] doc_mode → diagram_focus (id={diagram_id}, inner={diagram_focus_sm.state})")
        await params.result_callback(
            f"OK: Diagram Focus Mode active for '{diagram_id}'. "
            f"Describe changes to update the diagram, or say 'exit diagram mode' when done."
        )

    async def exit_diagram_focus(params: FunctionCallParams):
        """Exit Diagram Focus Mode and return to the documentation view.

        Call this when the user says 'exit diagram mode', 'done', or 'save and exit'.
        """
        session = doc_sm.session
        if session.state.value != "diagram_focus":
            await params.result_callback(
                f"INVALID_STATE: exit_diagram_focus requires diagram_focus "
                f"(current: {session.state.value})."
            )
            return

        diagram_id = diagram_focus_sm.session.diagram_id or session.active_diagram_id
        try:
            from doc_state import StateMachineError
            doc_sm.exit_diagram_focus()
            diagram_focus_sm.exit()
        except (StateMachineError, Exception) as e:
            await params.result_callback(f"ERROR: {e}")
            return

        msg = ServerMessage(data={"type": "diagram-focus-exited", "diagram_id": diagram_id})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[DIAGRAM FOCUS] diagram_focus → doc_mode (id={diagram_id})")
        await params.result_callback(
            f"OK: Diagram Focus Mode exited. Documentation view restored."
        )

    async def revert_diagram_edit(params: FunctionCallParams):
        """Roll back the most recent diagram edit, restoring the previous version.

        Call this in Diagram Focus Mode when the user says the change was not okay
        ('no', 'undo', 'revert', 'go back'). Restores the diagram as it was before the
        last update_diagram and re-renders it. Single-level undo.
        """
        session = doc_sm.session
        if session.state.value != "diagram_focus":
            await params.result_callback(f"INVALID_STATE: revert_diagram_edit requires diagram_focus.")
            return
        vi = session.version_info
        if vi is None:
            await params.result_callback(f"ERROR: No version info in current session.")
            return

        fs = diagram_focus_sm.session
        diagram_id = fs.diagram_id or session.active_diagram_id
        prev_source = diagram_focus_sm.revert()
        if prev_source is None:
            await params.result_callback(f"NOTHING_TO_REVERT: There's no earlier version to go back to. Describe the change you want instead.")
            return

        try:
            current = vi.document_md.read_text() if vi.document_md.exists() else ""
            new_doc, found = _update_diagram_in_doc(current, diagram_id, prev_source)
            if found:
                atomic_write(vi.document_md, new_doc)
            session.last_valid_diagrams[diagram_id] = prev_source

            await task.queue_frames([
                OutputTransportMessageUrgentFrame(
                    message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
                ),
                OutputTransportMessageUrgentFrame(
                    message=ServerMessage(data={
                        "type": "diagram-focus-updated",
                        "diagram_id": diagram_id,
                        "mermaid_source": prev_source,
                        "reverted": True,
                    }).model_dump()
                ),
            ])
            logger.info(f"[DIAGRAM FOCUS] Reverted edit for id={diagram_id}")
            await params.result_callback(
                "OK: Rolled back to the previous version of the diagram. "
                "Describe another change, or say 'exit diagram mode' when done."
            )
        except Exception as e:
            logger.error(f"[DIAGRAM FOCUS] revert failed: {e}")
            await params.result_callback(f"WRITE_ERROR: {e}")

    return {
        "move_diagram": move_diagram,
        "enter_diagram_focus": enter_diagram_focus,
        "exit_diagram_focus": exit_diagram_focus,
        "revert_diagram_edit": revert_diagram_edit,
    }
