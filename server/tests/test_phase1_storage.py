"""Phase 1 — Milestone 1.1: Storage layer tests."""

import json
import time
from pathlib import Path

import pytest

from doc_storage import (
    create_project,
    load_project,
    load_version,
    create_next_version,
    sanitize_slug,
)


@pytest.fixture
def docs_root(tmp_path):
    return tmp_path / "docs"


# ── sanitize_slug ─────────────────────────────────────────────────────────────

def test_sanitize_slug_basic():
    assert sanitize_slug("My Project") == "my-project"

def test_sanitize_slug_path_traversal():
    slug = sanitize_slug("../../etc/passwd")
    assert ".." not in slug
    assert "/" not in slug
    assert slug  # not empty

def test_sanitize_slug_shell_metacharacters():
    slug = sanitize_slug("foo; rm -rf /")
    assert ";" not in slug
    assert "/" not in slug

def test_sanitize_slug_empty_fallback():
    assert sanitize_slug("!!!") == "untitled"

def test_sanitize_slug_unicode_stripped():
    slug = sanitize_slug("résumé project")
    assert all(c.isascii() for c in slug)


# ── create_project ────────────────────────────────────────────────────────────

def test_create_project_directory_structure(docs_root):
    project_info, version_info = create_project("My Project", root=docs_root)

    assert project_info.project_dir.is_dir()
    assert version_info.version_dir.is_dir()
    assert version_info.document_md.exists()
    assert version_info.transcript_md.exists()
    assert version_info.speakers_json.exists()
    assert version_info.diagrams_dir.is_dir()
    assert version_info.artifacts_dir.is_dir()
    assert (project_info.project_dir / "project.json").exists()
    assert version_info.manifest_path().exists()

def test_create_project_version_is_zero(docs_root):
    _, version_info = create_project("Alpha", root=docs_root)
    assert version_info.version == 0

def test_create_project_manifest_fields(docs_root):
    _, version_info = create_project("Beta", root=docs_root)
    manifest = json.loads(version_info.manifest_path().read_text())
    assert manifest["version"] == 0
    assert manifest["derived_from"] is None
    assert "created_at" in manifest
    assert "document_md" in manifest
    assert "transcript_md" in manifest

def test_create_project_speakers_json_empty_object(docs_root):
    _, version_info = create_project("Gamma", root=docs_root)
    speakers = json.loads(version_info.speakers_json.read_text())
    assert speakers == {}

def test_create_project_path_traversal_rejected(docs_root):
    project_info, _ = create_project("../../etc/passwd", root=docs_root)
    # The resulting directory must be inside docs_root
    assert str(project_info.project_dir).startswith(str(docs_root))

def test_create_project_duplicate_slug_counter(docs_root):
    info1, _ = create_project("duplicate", root=docs_root)
    info2, _ = create_project("duplicate", root=docs_root)
    assert info1.slug != info2.slug
    assert info2.slug.startswith("duplicate")

def test_create_project_document_md_has_title_heading(docs_root):
    # document.md is seeded with a # heading from the topic name on create.
    _, version_info = create_project("Empty Doc", root=docs_root)
    content = version_info.document_md.read_text()
    assert "Empty Doc" in content


# ── load_project / load_version ───────────────────────────────────────────────

def test_load_project_round_trip(docs_root):
    project_info, _ = create_project("Round Trip", root=docs_root)
    loaded = load_project(project_info.slug, root=docs_root)
    assert loaded is not None
    assert loaded.slug == project_info.slug
    assert loaded.display_name == "Round Trip"
    assert loaded.current_version == 0

def test_load_project_missing_returns_none(docs_root):
    assert load_project("nonexistent", root=docs_root) is None

def test_load_version_round_trip(docs_root):
    project_info, version_info = create_project("Version Load", root=docs_root)
    loaded = load_version(project_info.project_dir, 0)
    assert loaded is not None
    assert loaded.version == 0
    assert loaded.document_md.exists()

def test_load_version_missing_returns_none(docs_root):
    project_info, _ = create_project("No Version 5", root=docs_root)
    assert load_version(project_info.project_dir, 5) is None


# ── create_next_version ───────────────────────────────────────────────────────

def test_create_next_version_increments(docs_root):
    project_info, _ = create_project("Versioned", root=docs_root)
    v1 = create_next_version(project_info.project_dir, derived_from=0)
    assert v1.version == 1
    assert v1.version_dir.is_dir()
    assert v1.derived_from == 0

def test_create_next_version_manifest_lineage(docs_root):
    project_info, _ = create_project("Lineage", root=docs_root)
    v1 = create_next_version(project_info.project_dir, derived_from=0)
    manifest = json.loads(v1.manifest_path().read_text())
    assert manifest["derived_from"] == 0
