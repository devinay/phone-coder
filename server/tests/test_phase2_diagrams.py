"""Phase 2 — Diagram helper unit tests.

Tests cover:
  - _validate_mermaid_source
  - _insert_diagram_in_doc / _update_diagram_in_doc / _extract_diagram_source
  - MERMAID_SUPPORTED_TYPES / MERMAID_UNSUPPORTED_TYPES classification
  - DocStateMachine diagram_focus transitions (enter / exit / invalid)
"""

import os
import sys
from types import SimpleNamespace

# Allow importing from server/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bot import (
    MERMAID_SUPPORTED_TYPES,
    MERMAID_UNSUPPORTED_TYPES,
    _build_diagram_block,
    _embed_image_in_node,
    _ensure_writable_version,
    _extract_diagram_source,
    _insert_diagram_in_doc,
    _mark_doc_session_edited,
    _strip_generated_sections,
    _update_diagram_in_doc,
    _validate_mermaid_source,
)
from doc_state import DocModeState, DocStateMachine, StateMachineError
from doc_storage import atomic_write, create_project, load_project

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


def test_update_embedded_image_preserves_surrounding_markdown():
    doc = (
        "# Doc\n\n"
        "Intro text that must not disappear.\n\n"
        "<!-- diagram-id: d1 -->\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  A[Start] --> B[End]\n"
        "```\n\n"
        "Conclusion text that must stay.\n"
    )
    image_source, node_found = _embed_image_in_node(
        "flowchart LR\n  A[Start] --> B[End]",
        "B",
        "/api/docs/demo/version/1/images/d1-B.png",
        40,
    )
    assert node_found

    new_doc, doc_found = _update_diagram_in_doc(doc, "d1", image_source)

    assert doc_found
    assert "Intro text that must not disappear." in new_doc
    assert "Conclusion text that must stay." in new_doc
    assert '<img src="/api/docs/demo/version/1/images/d1-B.png" width="40"/>' in new_doc


def test_ensure_writable_version_forks_opened_existing_once(tmp_path):
    project_info, v0 = create_project("Open Existing", root=tmp_path)
    atomic_write(v0.document_md, "# Open Existing\n\nKeep version zero.\n")
    session = SimpleNamespace(
        opened_existing=True,
        forked=False,
        version_info=v0,
        version=v0.version,
        has_edits=False,
    )

    v1 = _ensure_writable_version(session)

    assert v1.version == 1
    assert session.forked
    assert session.version == 1
    assert "Keep version zero." in v1.document_md.read_text()
    assert v0.document_md.read_text() == "# Open Existing\n\nKeep version zero.\n"
    loaded = load_project(project_info.slug, root=tmp_path)
    assert loaded is not None
    assert loaded.current_version == 1

    same_v1 = _ensure_writable_version(session)
    assert same_v1.version == 1
    assert not (project_info.project_dir / "version_2").exists()


def test_mark_doc_session_edited_records_diagram_edits():
    session = SimpleNamespace(has_edits=False)

    _mark_doc_session_edited(session)

    assert session.has_edits


def test_strip_generated_sections_removes_prior_summary_and_transcript_once():
    doc = (
        "# Doc\n\n"
        "## Summary\n\n"
        "Old generated summary.\n\n"
        "---\n\n"
        "## Main Content\n\n"
        "User-authored content stays.\n\n"
        "---\n\n"
        "<details>\n"
        "<summary>Transcript</summary>\n\n"
        "Old transcript.\n"
        "</details>\n"
    )

    stripped = _strip_generated_sections(doc)

    assert "Old generated summary" not in stripped
    assert "<summary>Transcript</summary>" not in stripped
    assert "## Main Content" in stripped
    assert "User-authored content stays." in stripped


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
