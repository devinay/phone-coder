"""Phase 1 — Git-backed storage layer tests.

Covers git_storage: project creation, load/list round-trips, document/diagram
saves (commit + push handling), and the slug/dir/filename/heading alignment.
"""

import subprocess

import pytest

from git_storage import (
    _safe_child,
    create_project,
    git_root,
    list_projects,
    load_document,
    load_project,
    save_diagram,
    save_document,
)
from poc.atomic_write import sanitize_slug


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real git repo as VOICE_COCKPIT_GIT_ROOT (no remote)."""
    repo = tmp_path / "docs-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@local")
    _git(repo, "config", "user.name", "Test")
    monkeypatch.setenv("VOICE_COCKPIT_GIT_ROOT", str(repo))
    return repo


# ── sanitize_slug ─────────────────────────────────────────────────────────────

def test_sanitize_slug_basic():
    assert sanitize_slug("My Project") == "my-project"

def test_sanitize_slug_path_traversal():
    slug = sanitize_slug("../../etc/passwd")
    assert ".." not in slug and "/" not in slug and slug

def test_sanitize_slug_empty_fallback():
    assert sanitize_slug("!!!") == "untitled"


# ── git_root validation ───────────────────────────────────────────────────────

def test_git_root_unset_raises(monkeypatch):
    monkeypatch.delenv("VOICE_COCKPIT_GIT_ROOT", raising=False)
    with pytest.raises(ValueError, match="VOICE_COCKPIT_GIT_ROOT"):
        git_root()

def test_git_root_not_a_repo_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_COCKPIT_GIT_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="Git repository not found"):
        git_root()

def test_git_root_resolves_when_valid(git_repo):
    assert git_root() == git_repo.resolve()


def test_safe_child_rejects_symlink_escape(tmp_path):
    root = tmp_path / "docs"
    outside = tmp_path / "docs-other"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes docs root"):
        _safe_child(root, "escape")


# ── create_project ─────────────────────────────────────────────────────────────

def test_create_project_layout_and_alignment(git_repo):
    project, doc = create_project("My Project")
    # slug == dir name == md filename stem
    assert project.slug == "my-project"
    assert doc.project_dir.name == "my-project"
    assert doc.document_md.name == "my-project.md"
    # heading matches the display name
    assert doc.document_md.read_text().startswith("# My Project")
    # supporting files exist
    assert doc.transcript_md.exists()
    assert doc.speakers_json.exists()
    assert (doc.project_dir / "metadata.json").exists()

def test_create_project_commits_to_git(git_repo):
    create_project("Alpha")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout
    assert "Create project: Alpha" in log

def test_create_project_duplicate_slug_suffix(git_repo):
    p1, _ = create_project("Dup")
    p2, _ = create_project("Dup")
    assert p1.slug == "dup"
    assert p2.slug == "dup-1"


# ── load / list ────────────────────────────────────────────────────────────────

def test_load_project_round_trip(git_repo):
    create_project("Round Trip")
    loaded = load_project("round-trip")
    assert loaded is not None
    assert loaded.slug == "round-trip"
    assert loaded.display_name == "Round Trip"

def test_load_project_missing_returns_none(git_repo):
    assert load_project("nope") is None

def test_load_document_round_trip(git_repo):
    _, doc = create_project("Doc Load")
    loaded = load_document(doc.project_dir)
    assert loaded is not None
    assert loaded.document_md.name == "doc-load.md"

def test_list_projects(git_repo):
    create_project("One")
    create_project("Two")
    slugs = {p["slug"] for p in list_projects()}
    assert {"one", "two"} <= slugs


# ── save_document ──────────────────────────────────────────────────────────────

def test_save_document_commits_actual_files(git_repo):
    _, doc = create_project("Save Test")
    result = save_document(doc, "# Save Test\n\nBody.\n", "transcript", {}, "Edit body")
    assert result.commit_sha  # a commit happened
    # committed_files reflects what actually changed (the md at minimum)
    assert any(f.endswith("save-test.md") for f in result.committed_files)

def test_save_document_push_warns_without_remote(git_repo):
    _, doc = create_project("No Remote")
    result = save_document(doc, "# No Remote\n\nx\n", "t", {}, "msg")
    # No origin configured → push fails gracefully, save still succeeds locally
    assert result.push_success is False
    assert result.warning_message
    assert result.commit_sha

def test_save_document_no_change_is_not_an_error(git_repo):
    _, doc = create_project("Idempotent")
    content = doc.document_md.read_text()
    # Re-saving identical content → nothing to commit, empty sha, no crash
    result = save_document(doc, content, "", {}, "noop")
    assert result.commit_sha == ""
    assert result.committed_files == []


def test_save_document_excludes_session_and_gitignore_files(git_repo):
    _, doc = create_project("Exclude Junk")
    (doc.project_dir / ".gitignore").write_text("*.tmp\n")
    session_dir = doc.project_dir / ".session"
    session_dir.mkdir()
    (session_dir / "snapshot.png").write_bytes(b"png")

    result = save_document(doc, "# Exclude Junk\n\nBody.\n", "", {}, "Save without junk")
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert not any(".session" in path for path in result.committed_files)
    assert not any(path.endswith(".gitignore") for path in result.committed_files)
    assert not any(".session" in path for path in tracked)
    assert f"{doc.slug}/.gitignore" not in tracked


# ── save_diagram ───────────────────────────────────────────────────────────────

def test_save_diagram_commits_embedded_image(git_repo):
    _, doc = create_project("With Image")
    # Simulate an embedded image landing in the project's images/ dir.
    (doc.project_dir / "images").mkdir(exist_ok=True)
    (doc.project_dir / "images" / "icon.png").write_bytes(b"png")
    new_md = doc.document_md.read_text() + "\n![icon](images/icon.png)\n"
    result = save_diagram(doc, new_md, "Add image")
    # Whole-folder staging means the image is committed alongside the markdown.
    assert any(f.endswith("icon.png") for f in result.committed_files)
    assert any(f.endswith("with-image.md") for f in result.committed_files)
