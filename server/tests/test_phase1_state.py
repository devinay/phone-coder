"""Phase 1 — Milestone 1.2: State machine tests."""

import pytest

from doc_state import (
    DocStateMachine,
    DocModeState,
    StateMachineError,
    INVALID_STATE,
    ALREADY_ACTIVE,
)


@pytest.fixture
def sm():
    return DocStateMachine()


@pytest.fixture
def fake_version_info(tmp_path):
    """Minimal stand-in for VersionInfo."""
    class FakeVI:
        version = 0
        version_dir = tmp_path / "version_0"
    return FakeVI()


# ── Initial state ─────────────────────────────────────────────────────────────

def test_initial_state_is_shell(sm):
    assert sm.state == DocModeState.SHELL


# ── enter_doc_mode ────────────────────────────────────────────────────────────

def test_enter_doc_mode_from_shell(sm, fake_version_info, tmp_path):
    opened = sm.enter_doc_mode("my-project", fake_version_info, tmp_path)
    assert opened is True
    assert sm.state == DocModeState.DOC_MODE
    assert sm.session.project_slug == "my-project"

def test_enter_doc_mode_idempotent(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    result = sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    assert result is False  # ALREADY_ACTIVE — no-op
    assert sm.state == DocModeState.DOC_MODE

def test_enter_doc_mode_idempotent_no_duplicate_dir(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    # Still exactly one session open; state unchanged
    assert sm.session.project_slug == "proj"

def test_enter_doc_mode_from_diagram_focus_rejected(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    sm.session.state = DocModeState.DIAGRAM_FOCUS  # force to non-shell state
    with pytest.raises(StateMachineError) as exc_info:
        sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    assert exc_info.value.code == INVALID_STATE


# ── exit_doc_mode ─────────────────────────────────────────────────────────────

def test_exit_doc_mode_from_doc_mode(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    sm.exit_doc_mode()
    assert sm.state == DocModeState.SAVING

def test_exit_doc_mode_then_complete_save(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    sm.exit_doc_mode()
    sm.complete_save()
    assert sm.state == DocModeState.SHELL
    assert sm.session.project_slug is None
    assert sm.session.doc_writer is None

def test_exit_doc_mode_from_shell_rejected(sm):
    with pytest.raises(StateMachineError) as exc_info:
        sm.exit_doc_mode()
    assert exc_info.value.code == INVALID_STATE

def test_exit_diagram_focus_from_shell_rejected(sm):
    """exit_diagram_focus from shell must be rejected (Phase 1 gate check)."""
    with pytest.raises(StateMachineError) as exc_info:
        sm._check_allowed("exit_diagram_focus")
    assert exc_info.value.code == INVALID_STATE


# ── doc_writer initialised on entry ──────────────────────────────────────────

def test_doc_writer_created_on_enter(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    assert sm.session.doc_writer is not None

def test_doc_writer_cleared_on_complete_save(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    sm.exit_doc_mode()
    sm.complete_save()
    assert sm.session.doc_writer is None


# ── error_recovery ────────────────────────────────────────────────────────────

def test_recover_to_shell_resets_state(sm, fake_version_info, tmp_path):
    sm.enter_doc_mode("proj", fake_version_info, tmp_path)
    sm.enter_error_recovery()
    assert sm.state == DocModeState.ERROR_RECOVERY
    sm.recover_to_shell()
    assert sm.state == DocModeState.SHELL
    assert sm.session.project_slug is None
