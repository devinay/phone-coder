"""Diagram Focus Mode — inner state machine.

Sits inside doc_mode. Owns the lifecycle of a single diagram editing session.

Top-level states:
  idle      — not in focus (default)
  viewing   — diagram displayed fullscreen, waiting for user command
  editing   — user described a change, LLM is generating new Mermaid source
  saving    — update_diagram called, write in progress

Image search sub-states (compound state on session, independent of top-level):
  image_search_state = None      — no image search in progress
  image_search_state = searching — DuckDuckGo call in flight, downloading images
  image_search_state = selecting — thumbnails displayed, waiting for user pick
  image_search_state = sizing    — image embedded, user confirming/adjusting size
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class DiagramFocusState(str, Enum):
    IDLE    = "idle"
    VIEWING = "viewing"
    EDITING = "editing"
    SAVING  = "saving"


IMAGE_SEARCH_STATES = ("searching", "selecting", "sizing")


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
    pending_description: str = ""

    # Image search compound state
    image_search_state: str | None = None       # None | "searching" | "selecting" | "sizing"
    image_search_element_id: str | None = None  # Mermaid node ID being replaced
    image_search_dir: Path | None = None        # /tmp/cockpit-images/<session>/
    image_paths: list[Path] = field(default_factory=list)  # up to 5 downloaded paths
    selected_image_path: Path | None = None     # path of chosen image (permanent)
    current_image_width: int = 40               # px width for Mermaid <img> tag

    @property
    def in_image_search(self) -> bool:
        return self.image_search_state is not None


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

    # ── Top-level focus lifecycle ─────────────────────────────────────────────

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
        """Transition any active state → idle. Cleans up image search state. Returns diagram_id."""
        diagram_id = self._session.diagram_id
        self._cleanup_image_search()
        self._session = DiagramFocusSession()
        return diagram_id

    # ── Image search sub-state lifecycle ─────────────────────────────────────

    def begin_image_search(self, element_id: str, search_dir: Path) -> None:
        """Enter image_search / searching sub-state."""
        if not self.is_active:
            raise DiagramFocusError("INVALID_STATE", "Not in diagram focus mode.")
        if self._session.in_image_search:
            raise DiagramFocusError("ALREADY_ACTIVE", "Image search already in progress.")
        self._session.image_search_state = "searching"
        self._session.image_search_element_id = element_id
        self._session.image_search_dir = search_dir
        self._session.image_paths = []
        self._session.selected_image_path = None
        self._session.current_image_width = 40

    def set_image_results(self, paths: list[Path]) -> None:
        """Transition searching → selecting once images are downloaded."""
        if self._session.image_search_state != "searching":
            raise DiagramFocusError("INVALID_STATE", "Not in searching state.")
        self._session.image_paths = paths
        self._session.image_search_state = "selecting"

    def select_image(self, path: Path) -> None:
        """Transition selecting → sizing once user picks an image."""
        if self._session.image_search_state != "selecting":
            raise DiagramFocusError("INVALID_STATE", "Not in selecting state.")
        self._session.selected_image_path = path
        self._session.current_image_width = 40
        self._session.image_search_state = "sizing"

    def set_image_width(self, width: int) -> None:
        """Update width while in sizing state."""
        if self._session.image_search_state != "sizing":
            raise DiagramFocusError("INVALID_STATE", "Not in sizing state.")
        self._session.current_image_width = max(20, min(200, width))

    def complete_image_search(self) -> None:
        """Exit image search sub-state → back to viewing."""
        self._cleanup_image_search()
        self._session.image_search_state = None
        self._session.state = DiagramFocusState.VIEWING

    def cancel_image_search(self) -> None:
        """Cancel image search, delete all temp files, return to viewing."""
        self._cleanup_image_search()
        self._session.image_search_state = None
        self._session.state = DiagramFocusState.VIEWING

    def _cleanup_image_search(self) -> None:
        """Delete all temp image files that haven't been selected."""
        import shutil
        d = self._session.image_search_dir
        if d and d.exists():
            try:
                shutil.rmtree(d)
            except Exception:
                pass
        self._session.image_search_dir = None
        self._session.image_paths = []

    # ── Width helpers ─────────────────────────────────────────────────────────

    WIDTH_STEPS = [20, 30, 40, 60, 80, 100, 120, 160, 200]

    def width_bigger(self) -> int:
        w = self._session.current_image_width
        for step in self.WIDTH_STEPS:
            if step > w:
                self._session.current_image_width = step
                return step
        return w

    def width_smaller(self) -> int:
        w = self._session.current_image_width
        for step in reversed(self.WIDTH_STEPS):
            if step < w:
                self._session.current_image_width = step
                return step
        return w
