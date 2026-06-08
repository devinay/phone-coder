"""Phase 1 — Mode state machine for the Voice Coding Cockpit.

The server is authoritative for state. The browser reflects state via RTVI events.

States:
  shell           — default terminal/coding mode
  doc_mode        — documentation editor overlay active
  diagram_focus   — single diagram open for fullscreen editing
  review_mode     — review pane active for a selected project version
  saving          — a write is in progress (transient)
  error_recovery  — a failed transition or write is being recovered

Phase 1 implements: shell ↔ doc_mode transitions only.
Remaining states are defined so callers can reference them; their transitions
will be enforced as Phase 2–4 add the corresponding tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING


class DocModeState(str, Enum):
    SHELL = "shell"
    DOC_MODE = "doc_mode"
    DIAGRAM_FOCUS = "diagram_focus"
    REVIEW_MODE = "review_mode"
    SAVING = "saving"
    ERROR_RECOVERY = "error_recovery"


# Error codes returned by transition methods
ALREADY_ACTIVE = "ALREADY_ACTIVE"
INVALID_STATE = "INVALID_STATE"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
WRITE_ERROR = "WRITE_ERROR"
NOT_ACTIVE = "NOT_ACTIVE"
SAVE_FAILED = "SAVE_FAILED"
ID_NOT_FOUND = "ID_NOT_FOUND"
VERSION_NOT_FOUND = "VERSION_NOT_FOUND"


@dataclass
class StateMachineError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# Allowed source states for each transition
_ALLOWED_SOURCES: dict[str, list[DocModeState]] = {
    "enter_doc_mode":       [DocModeState.SHELL, DocModeState.DOC_MODE],
    "exit_doc_mode":        [DocModeState.DOC_MODE],
    "enter_diagram_focus":  [DocModeState.DOC_MODE],
    "exit_diagram_focus":   [DocModeState.DIAGRAM_FOCUS],
    "enter_review_mode":    [DocModeState.SHELL],
    "exit_review_mode":     [DocModeState.REVIEW_MODE],
}


@dataclass
class DocSession:
    """Server-side session state for an active documentation session."""
    state: DocModeState = DocModeState.SHELL
    project_slug: str | None = None
    project_dir: object = None      # Path | None
    version: int | None = None
    version_info: object = None     # VersionInfo | None
    speaker_map: dict[str, str] = field(default_factory=dict)
    active_diagram_id: str | None = None
    last_valid_diagrams: dict = field(default_factory=dict)  # diagram_id → last valid mermaid source
    has_edits: bool = False          # True once any write_to_doc / insert_diagram / update_diagram called
    opened_existing: bool = False    # True if this session opened an existing version (fork-on-edit)
    forked: bool = False             # True once a copy-on-write fork has been created this session
    _doc_writer: object = None       # DocWriter | None

    @property
    def doc_writer(self):
        return self._doc_writer

    @doc_writer.setter
    def doc_writer(self, value):
        self._doc_writer = value


class DocStateMachine:
    """Validates and applies mode transitions.

    Usage:
        sm = DocStateMachine()
        sm.transition("enter_doc_mode", session)   # raises StateMachineError on invalid
    """

    def __init__(self):
        self._session = DocSession()

    @property
    def session(self) -> DocSession:
        return self._session

    @property
    def state(self) -> DocModeState:
        return self._session.state

    def _check_allowed(self, action: str) -> None:
        allowed = _ALLOWED_SOURCES.get(action)
        if allowed is None:
            raise StateMachineError(INVALID_STATE, f"Unknown action '{action}'")
        if self._session.state not in allowed:
            raise StateMachineError(
                INVALID_STATE,
                f"Cannot '{action}' from state '{self._session.state.value}'. "
                f"Allowed source states: {[s.value for s in allowed]}",
            )

    def enter_doc_mode(self, project_slug: str, version_info, project_dir, opened_existing: bool = False) -> bool:
        """Transition to doc_mode. Idempotent if already in doc_mode.

        Returns True if a new session was opened, False if already active (no-op).
        Raises StateMachineError on invalid source state.
        """
        if self._session.state == DocModeState.DOC_MODE:
            return False  # ALREADY_ACTIVE — idempotent no-op

        self._check_allowed("enter_doc_mode")

        from doc_writer import DocWriter
        self._session.project_slug = project_slug
        self._session.project_dir = project_dir
        self._session.version_info = version_info
        self._session.version = version_info.version if version_info else None
        self._session._doc_writer = DocWriter(title=project_slug or "Session")
        self._session.speaker_map = {"controller": "Controller", "0": "User"}
        self._session.opened_existing = opened_existing
        self._session.forked = False
        self._session.state = DocModeState.DOC_MODE
        return True

    def exit_doc_mode(self) -> None:
        """Transition out of doc_mode → saving → shell.

        Raises StateMachineError if not in doc_mode.
        """
        self._check_allowed("exit_doc_mode")
        self._session.state = DocModeState.SAVING

    def complete_save(self) -> None:
        """Called after a successful save to advance from saving → shell."""
        self._session.state = DocModeState.SHELL
        self._session.project_slug = None
        self._session.project_dir = None
        self._session.version_info = None
        self._session.version = None
        self._session._doc_writer = None
        self._session.speaker_map = {}
        self._session.active_diagram_id = None
        self._session.has_edits = False
        self._session.opened_existing = False
        self._session.forked = False

    def enter_diagram_focus(self, diagram_id: str) -> None:
        """Transition from doc_mode → diagram_focus for the given diagram.

        Raises StateMachineError if not in doc_mode.
        """
        self._check_allowed("enter_diagram_focus")
        self._session.active_diagram_id = diagram_id
        self._session.state = DocModeState.DIAGRAM_FOCUS

    def exit_diagram_focus(self) -> None:
        """Transition from diagram_focus → doc_mode.

        Raises StateMachineError if not in diagram_focus.
        """
        self._check_allowed("exit_diagram_focus")
        self._session.active_diagram_id = None
        self._session.state = DocModeState.DOC_MODE

    def enter_error_recovery(self) -> None:
        self._session.state = DocModeState.ERROR_RECOVERY

    def recover_to_shell(self) -> None:
        """Clear all session state and return to shell."""
        self._session = DocSession()
