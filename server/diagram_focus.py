"""Diagram Focus Mode — inner state machine.

Sits inside doc_mode. Owns the lifecycle of a single diagram editing session.

States:
  idle      — not in focus (default)
  viewing   — diagram displayed fullscreen, waiting for user command
  editing   — user described a change, LLM is generating new Mermaid source
  saving    — update_diagram called, write in progress
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class DiagramFocusState(str, Enum):
    IDLE    = "idle"
    VIEWING = "viewing"
    EDITING = "editing"
    SAVING  = "saving"


class DiagramFocusError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class DiagramFocusSession:
    state: DiagramFocusState = DiagramFocusState.IDLE
    diagram_id: str | None = None
    current_source: str | None = None
    # Accumulates description tokens from the user while in editing state
    pending_description: str = ""


class DiagramFocusStateMachine:
    """Manages the inner states of a single diagram editing session."""

    def __init__(self):
        self._session = DiagramFocusSession()

    @property
    def session(self) -> DiagramFocusSession:
        return self._session

    @property
    def state(self) -> DiagramFocusState:
        return self._session.state

    @property
    def is_active(self) -> bool:
        return self._session.state != DiagramFocusState.IDLE

    def enter(self, diagram_id: str, mermaid_source: str) -> None:
        """Transition idle → viewing."""
        if self._session.state != DiagramFocusState.IDLE:
            raise DiagramFocusError("ALREADY_ACTIVE", "Already in diagram focus mode.")
        self._session.diagram_id = diagram_id
        self._session.current_source = mermaid_source
        self._session.pending_description = ""
        self._session.state = DiagramFocusState.VIEWING

    def begin_edit(self, description: str = "") -> None:
        """Transition viewing → editing."""
        if self._session.state not in (DiagramFocusState.VIEWING, DiagramFocusState.EDITING):
            raise DiagramFocusError("INVALID_STATE", f"Cannot begin_edit from {self._session.state}")
        self._session.pending_description = description
        self._session.state = DiagramFocusState.EDITING

    def begin_save(self) -> None:
        """Transition editing → saving."""
        if self._session.state != DiagramFocusState.EDITING:
            raise DiagramFocusError("INVALID_STATE", f"Cannot begin_save from {self._session.state}")
        self._session.state = DiagramFocusState.SAVING

    def complete_save(self, new_source: str) -> None:
        """Transition saving → viewing, recording the new source."""
        if self._session.state != DiagramFocusState.SAVING:
            raise DiagramFocusError("INVALID_STATE", f"Cannot complete_save from {self._session.state}")
        self._session.current_source = new_source
        self._session.pending_description = ""
        self._session.state = DiagramFocusState.VIEWING

    def exit(self) -> str | None:
        """Transition any active state → idle. Returns the diagram_id."""
        diagram_id = self._session.diagram_id
        self._session = DiagramFocusSession()
        return diagram_id
