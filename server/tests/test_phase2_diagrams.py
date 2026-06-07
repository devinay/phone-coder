"""Phase 2 — Diagram helper unit tests.

Tests cover:
  - _validate_mermaid_source
  - _insert_diagram_in_doc / _update_diagram_in_doc / _extract_diagram_source
  - MERMAID_SUPPORTED_TYPES / MERMAID_UNSUPPORTED_TYPES classification
  - DocStateMachine diagram_focus transitions (enter / exit / invalid)
"""

import sys
import os

# Allow importing from server/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bot import (
    MERMAID_SUPPORTED_TYPES,
    MERMAID_UNSUPPORTED_TYPES,
    _validate_mermaid_source,
    _insert_diagram_in_doc,
    _update_diagram_in_doc,
    _extract_diagram_source,
    _build_diagram_block,
)
from doc_state import DocModeState, DocStateMachine, StateMachineError


# ── _validate_mermaid_source ──────────────────────────────────────────────────

def test_validate_sequence_diagram_valid():
    ok, err = _validate_mermaid_source("sequenceDiagram\n  A ->> B: Hi", "sequenceDiagram")
    assert ok
    assert err == ""


def test_validate_flowchart_valid():
    ok, err = _validate_mermaid_source("flowchart LR\n  A --> B", "flowchart")
    assert ok


def test_validate_flowchart_graph_alias():
    # "graph" is a valid alias for flowchart
    ok, err = _validate_mermaid_source("graph LR\n  A --> B", "flowchart")
    assert ok


def test_validate_wrong_type():
    ok, err = _validate_mermaid_source("classDiagram\n  class Foo {}", "sequenceDiagram")
    assert not ok
    assert "sequenceDiagram" in err


def test_validate_empty_source():
    ok, err = _validate_mermaid_source("", "flowchart")
    assert not ok


def test_validate_case_insensitive():
    ok, _ = _validate_mermaid_source("SequenceDiagram\n  A ->> B: hi", "sequenceDiagram")
    assert ok


# ── Type classification ───────────────────────────────────────────────────────

def test_xychart_beta_unsupported():
    assert "xychart-beta" in MERMAID_UNSUPPORTED_TYPES


def test_sankey_beta_unsupported():
    assert "sankey-beta" in MERMAID_UNSUPPORTED_TYPES


def test_flowchart_supported():
    assert "flowchart" in MERMAID_SUPPORTED_TYPES


def test_sequence_diagram_supported():
    assert "sequenceDiagram" in MERMAID_SUPPORTED_TYPES


def test_no_overlap_between_supported_and_unsupported():
    assert MERMAID_SUPPORTED_TYPES.isdisjoint(MERMAID_UNSUPPORTED_TYPES)


# ── _build_diagram_block ──────────────────────────────────────────────────────

def test_build_diagram_block_format():
    block = _build_diagram_block("auth-flow", "sequenceDiagram\n  A ->> B: hi")
    assert block.startswith("<!-- diagram-id: auth-flow -->")
    assert "```mermaid" in block
    assert "sequenceDiagram" in block
    assert block.endswith("```")


# ── _insert_diagram_in_doc ────────────────────────────────────────────────────

def test_insert_appends_when_no_placeholder():
    doc = "# My Doc\n\nSome content.\n"
    new = _insert_diagram_in_doc(doc, "seq-1", "sequenceDiagram\n  A ->> B: Hi", None)
    assert "<!-- diagram-id: seq-1 -->" in new
    assert "sequenceDiagram" in new
    assert "My Doc" in new


def test_insert_replaces_placeholder():
    doc = "# Doc\n\n<!-- diagram: user login flow -->\n\nMore text.\n"
    new = _insert_diagram_in_doc(doc, "auth-flow", "sequenceDiagram\n  A ->> B: Login", "user login flow")
    assert "<!-- diagram-id: auth-flow -->" in new
    assert "<!-- diagram: user login flow -->" not in new


def test_insert_multiple_diagrams_distinct_ids():
    doc = "# Doc\n"
    doc = _insert_diagram_in_doc(doc, "diag-1", "flowchart LR\n  A --> B", None)
    doc = _insert_diagram_in_doc(doc, "diag-2", "sequenceDiagram\n  A ->> B: Hi", None)
    assert "<!-- diagram-id: diag-1 -->" in doc
    assert "<!-- diagram-id: diag-2 -->" in doc
    # Content should not bleed between blocks
    assert doc.index("diag-1") < doc.index("diag-2")


# ── _extract_diagram_source ───────────────────────────────────────────────────

def test_extract_existing_diagram():
    doc = "# Doc\n\n<!-- diagram-id: auth-flow -->\n```mermaid\nsequenceDiagram\n  A ->> B: Hi\n```\n"
    src = _extract_diagram_source(doc, "auth-flow")
    assert src is not None
    assert "sequenceDiagram" in src


def test_extract_missing_diagram_returns_none():
    doc = "# Doc\n\nNo diagrams here.\n"
    assert _extract_diagram_source(doc, "missing-id") is None


def test_extract_correct_diagram_from_multi():
    doc = "# Doc\n"
    doc = _insert_diagram_in_doc(doc, "diag-1", "flowchart LR\n  A --> B", None)
    doc = _insert_diagram_in_doc(doc, "diag-2", "sequenceDiagram\n  A ->> B: Hi", None)
    src1 = _extract_diagram_source(doc, "diag-1")
    src2 = _extract_diagram_source(doc, "diag-2")
    assert src1 is not None and "flowchart" in src1
    assert src2 is not None and "sequenceDiagram" in src2
    # No content bleed
    assert "sequenceDiagram" not in src1
    assert "flowchart" not in src2


# ── _update_diagram_in_doc ────────────────────────────────────────────────────

def test_update_existing_diagram():
    doc = "# Doc\n\n<!-- diagram-id: auth-flow -->\n```mermaid\nsequenceDiagram\n  A ->> B: Hi\n```\n"
    new_src = "sequenceDiagram\n  User ->> Server: Login\n  Server ->> User: OK"
    new_doc, found = _update_diagram_in_doc(doc, "auth-flow", new_src)
    assert found
    assert "User ->> Server" in new_doc
    assert "A ->> B" not in new_doc


def test_update_nonexistent_id_returns_not_found():
    doc = "# Doc\n\nNo diagrams.\n"
    new_doc, found = _update_diagram_in_doc(doc, "missing", "flowchart LR\n  A --> B")
    assert not found
    assert new_doc == doc


def test_update_preserves_other_diagrams():
    doc = "# Doc\n"
    doc = _insert_diagram_in_doc(doc, "d1", "flowchart LR\n  A --> B", None)
    doc = _insert_diagram_in_doc(doc, "d2", "sequenceDiagram\n  A ->> B: Hi", None)
    new_doc, found = _update_diagram_in_doc(doc, "d1", "flowchart TD\n  X --> Y")
    assert found
    assert "X --> Y" in new_doc
    assert "sequenceDiagram" in new_doc  # d2 unchanged


# ── DocStateMachine diagram_focus transitions ─────────────────────────────────

@pytest.fixture
def sm_in_doc_mode(tmp_path):
    sm = DocStateMachine()
    class FakeVI:
        version = 0
        version_dir = tmp_path / "v0"
    sm.enter_doc_mode(project_slug="test", version_info=FakeVI(), project_dir=tmp_path)
    return sm


def test_enter_diagram_focus_from_doc_mode(sm_in_doc_mode):
    sm = sm_in_doc_mode
    sm.enter_diagram_focus("auth-flow")
    assert sm.state == DocModeState.DIAGRAM_FOCUS
    assert sm.session.active_diagram_id == "auth-flow"


def test_exit_diagram_focus_returns_to_doc_mode(sm_in_doc_mode):
    sm = sm_in_doc_mode
    sm.enter_diagram_focus("auth-flow")
    sm.exit_diagram_focus()
    assert sm.state == DocModeState.DOC_MODE
    assert sm.session.active_diagram_id is None


def test_enter_diagram_focus_from_shell_raises(tmp_path):
    sm = DocStateMachine()
    with pytest.raises(StateMachineError):
        sm.enter_diagram_focus("auth-flow")


def test_exit_diagram_focus_from_doc_mode_raises(sm_in_doc_mode):
    sm = sm_in_doc_mode
    with pytest.raises(StateMachineError):
        sm.exit_diagram_focus()


def test_enter_diagram_focus_sets_active_id(sm_in_doc_mode):
    sm = sm_in_doc_mode
    sm.enter_diagram_focus("class-diagram-1")
    assert sm.session.active_diagram_id == "class-diagram-1"


def test_exit_diagram_focus_clears_active_id(sm_in_doc_mode):
    sm = sm_in_doc_mode
    sm.enter_diagram_focus("my-diag")
    sm.exit_diagram_focus()
    assert sm.session.active_diagram_id is None


def test_last_valid_diagrams_initialized_empty(sm_in_doc_mode):
    sm = sm_in_doc_mode
    assert sm.session.last_valid_diagrams == {}


def test_last_valid_diagrams_can_be_set(sm_in_doc_mode):
    sm = sm_in_doc_mode
    sm.session.last_valid_diagrams["auth-flow"] = "sequenceDiagram\n  A ->> B: hi"
    assert sm.session.last_valid_diagrams["auth-flow"].startswith("sequenceDiagram")
