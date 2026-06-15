"""Git-based storage layer for voice-driven documentation.

Single git repository model: VOICE_COCKPIT_GIT_ROOT contains one repo
for all projects. Each project is a folder within that repo.

Structure:
  VOICE_COCKPIT_GIT_ROOT/       ← Single git repository
    .git/
    .gitignore
    project-slug-1/
      <project-slug>.md         ← Main document (committed)
      transcript.md             ← Session transcript (committed)
      speakers.json             ← Speaker map (committed)
      diagrams/                 ← Diagram artifacts (committed)
      artifacts/                ← Other assets (committed)
      metadata.json             ← Project metadata (committed)
    project-slug-2/
      <project-slug-2>.md
      ...
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from poc.atomic_write import atomic_write, cleanup_stale_tmp, sanitize_slug


@dataclass
class DocumentInfo:
    """Document metadata within the git repository."""
    slug: str
    project_dir: Path
    created_at: float
    document_md: Path
    transcript_md: Path
    speakers_json: Path
    diagrams_dir: Path
    artifacts_dir: Path

    def metadata_path(self) -> Path:
        """Path to metadata.json (project info)."""
        return self.project_dir / "metadata.json"

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "created_at": self.created_at,
        }


@dataclass
class ProjectInfo:
    """Project summary for listing and loading."""
    display_name: str
    slug: str
    project_dir: Path
    created_at: float

    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "slug": self.slug,
            "created_at": self.created_at,
        }


@dataclass
class GitSaveResult:
    """Result of committing local files and attempting to push them."""

    commit_sha: str
    committed_files: list[str]
    push_success: bool
    push_message: str

    @property
    def warning_message(self) -> str:
        if self.push_success:
            return ""
        return f"Push not completed: {self.push_message}"

    @property
    def pushed_files(self) -> list[str]:
        return self.committed_files if self.push_success else []


def git_root() -> Path:
    """Return the configured git repository root, expanding ~ and env vars.

    Raises ValueError if the git repository does not exist.
    """
    raw = os.getenv("VOICE_COCKPIT_GIT_ROOT")
    if not raw:
        raise ValueError(
            "VOICE_COCKPIT_GIT_ROOT environment variable not set. "
            "Please configure it to point to your git repository."
        )
    root = Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
    if not (root / ".git").exists():
        raise ValueError(
            f"Git repository not found at {root}. "
            f"Please initialize it first: git init {root}"
        )
    return root


def docs_root() -> Path:
    """Alias for git_root() for backward compatibility."""
    return git_root()


def _safe_child(root: Path, slug: str) -> Path:
    """Resolve slug under root and assert it stays inside root.

    Both paths are resolved to handle symlinks correctly.
    """
    root_resolved = root.resolve()
    child = (root_resolved / slug).resolve()
    if not str(child).startswith(str(root_resolved)):
        raise ValueError(f"Path traversal detected: '{slug}' escapes docs root")
    return child


def _git_add_and_commit(
    repo_root: Path, message: str, paths: list[str] | None = None
) -> tuple[str, list[str]]:
    """Add and commit files to git.

    Returns (commit_sha, changed_files) where changed_files are the files *actually*
    changed in the resulting commit (not the intended staging list). If there was
    nothing to commit, returns ("", []).

    Raises RuntimeError on actual git failures (not "nothing to commit").
    """
    try:
        # Add all if paths is None, otherwise add specific paths
        if paths is None:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo_root,
                capture_output=True,
                check=True,
            )
        else:
            subprocess.run(
                ["git", "add"] + paths,
                cwd=repo_root,
                capture_output=True,
                check=True,
            )

        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            # Commit succeeded - get the SHA and the files actually changed in it.
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            changed = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            changed_files = [line for line in changed.splitlines() if line]
            return sha, changed_files
        elif "nothing to commit" in result.stderr or "nothing to commit" in result.stdout:
            # No changes to commit - this is not an error
            return "", []
        else:
            # Real error: missing user.name/email, merge state, hooks, etc.
            raise RuntimeError(f"Git commit failed: {result.stderr or result.stdout}")

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git commit failed: {e.stderr or e.stdout}")


def _git_push(repo_root: Path) -> tuple[bool, str]:
    """Try to push to remote. Returns (success, message).

    Uses git push without specifying branch - lets git handle upstream tracking.
    Falls back to attempting main/master if no upstream is configured.
    Returns (False, error_message) on failure instead of raising.
    """
    try:
        remotes = subprocess.run(
            ["git", "remote"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if remotes.returncode != 0:
            detail = (remotes.stderr or remotes.stdout or "could not read git remotes").strip()
            return False, f"Could not check git remotes: {detail}"
        if "origin" not in {r.strip() for r in remotes.stdout.splitlines()}:
            return (
                False,
                "No git remote named 'origin' is configured. Saved and committed locally; "
                "add one with: git remote add origin <url>",
            )

        # First, try push with no branch specified (respects upstream)
        result = subprocess.run(
            ["git", "push"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "Pushed successfully"

        # If that fails, try specific branches as fallback
        errors = [result.stderr or result.stdout or "git push failed"]
        for branch in ["main", "master"]:
            try:
                result = subprocess.run(
                    ["git", "push", "origin", branch],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    return True, f"Pushed to {branch}"
                errors.append(result.stderr or result.stdout or f"git push origin {branch} failed")
            except subprocess.TimeoutExpired:
                errors.append(f"git push origin {branch} timed out")
                continue

        # If we got here, push failed
        detail = " | ".join(e.strip() for e in errors if e.strip())
        return (
            False,
            "Saved and committed locally, but git push failed. "
            f"Details: {detail[:500]}",
        )

    except subprocess.TimeoutExpired:
        return False, "Saved and committed locally, but git push timed out."
    except Exception as e:
        return False, f"Saved and committed locally, but git push errored: {str(e)[:200]}"


def create_project(topic_name: str, root: Path | None = None) -> tuple[ProjectInfo, DocumentInfo]:
    """Create a new project folder within the git repository.

    Does NOT initialize a git repo (the root repo already exists).
    Returns (ProjectInfo, DocumentInfo) for the newly created project.
    Raises ValueError on path traversal or if slug is empty.
    """
    root = root or git_root()

    slug = sanitize_slug(topic_name)
    project_dir = _safe_child(root, slug)

    # Handle duplicate slug with a counter suffix
    base_slug = slug
    counter = 1
    while project_dir.exists():
        slug = f"{base_slug}-{counter}"
        project_dir = _safe_child(root, slug)
        counter += 1

    project_dir.mkdir(parents=True, exist_ok=False)
    cleanup_stale_tmp(project_dir)

    now = time.time()

    # Create document structure (no git init - use root repo)
    # Document filename matches slug, heading matches display name
    document_md = project_dir / f"{slug}.md"
    transcript_md = project_dir / "transcript.md"
    speakers_json = project_dir / "speakers.json"
    diagrams_dir = project_dir / "diagrams"
    artifacts_dir = project_dir / "artifacts"

    diagrams_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Create initial files
    # Heading matches the display name (topic_name), filename matches slug
    atomic_write(document_md, f"# {topic_name}\n")
    atomic_write(transcript_md, "")
    atomic_write(speakers_json, "{}")

    # Write metadata
    metadata = ProjectInfo(
        display_name=topic_name,
        slug=slug,
        project_dir=project_dir,
        created_at=now,
    )
    metadata_path = project_dir / "metadata.json"
    atomic_write(metadata_path, json.dumps(metadata.to_dict(), indent=2))

    # Stage the whole project folder so every artifact (md, transcript, speakers,
    # metadata, images, diagrams) is captured — not just a hand-listed subset.
    project_rel = project_dir.relative_to(root.resolve())
    _git_add_and_commit(root, f"Create project: {topic_name}", [str(project_rel)])

    doc_info = DocumentInfo(
        slug=slug,
        project_dir=project_dir,
        created_at=now,
        document_md=document_md,
        transcript_md=transcript_md,
        speakers_json=speakers_json,
        diagrams_dir=diagrams_dir,
        artifacts_dir=artifacts_dir,
    )

    return metadata, doc_info


def list_projects(root: Path | None = None) -> list[dict]:
    """Return a list of {slug, display_name} for all projects in the git repository."""
    root = root or git_root()
    if not root.exists():
        return []
    results = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        metadata_path = project_dir / "metadata.json"
        try:
            if metadata_path.exists():
                data = json.loads(metadata_path.read_text())
                results.append({
                    "slug": data.get("slug", project_dir.name),
                    "display_name": data.get("display_name", project_dir.name),
                })
        except Exception:
            pass
    return results


def load_project(slug: str, root: Path | None = None) -> ProjectInfo | None:
    """Load an existing project by slug. Returns None if not found."""
    root = root or git_root()
    slug = sanitize_slug(slug)
    project_dir = _safe_child(root, slug)

    if not project_dir.exists():
        return None

    metadata_path = project_dir / "metadata.json"
    if metadata_path.exists():
        try:
            data = json.loads(metadata_path.read_text())
            return ProjectInfo(
                display_name=data.get("display_name", slug),
                slug=slug,
                project_dir=project_dir,
                created_at=data.get("created_at", time.time()),
            )
        except Exception:
            pass

    # Fallback if metadata doesn't exist
    return ProjectInfo(
        display_name=slug,
        slug=slug,
        project_dir=project_dir,
        created_at=time.time(),
    )


def load_document(project_dir: Path) -> DocumentInfo | None:
    """Load document info from a project folder. Returns None if invalid."""
    # Infer slug from directory name
    slug = project_dir.name

    # Document file named after slug: <slug>.md
    document_md = project_dir / f"{slug}.md"
    if not document_md.exists():
        return None

    metadata_path = project_dir / "metadata.json"
    created_at = time.time()
    if metadata_path.exists():
        try:
            data = json.loads(metadata_path.read_text())
            created_at = data.get("created_at", created_at)
        except Exception:
            pass

    return DocumentInfo(
        slug=slug,
        project_dir=project_dir,
        created_at=created_at,
        document_md=document_md,
        transcript_md=project_dir / "transcript.md",
        speakers_json=project_dir / "speakers.json",
        diagrams_dir=project_dir / "diagrams",
        artifacts_dir=project_dir / "artifacts",
    )


def save_document(
    doc_info: DocumentInfo,
    document_content: str,
    transcript_content: str,
    speakers_data: dict | str,
    message: str = "Update documentation",
) -> GitSaveResult:
    """Save document files to project folder, commit to repo, and push to remote.

    Returns a GitSaveResult with committed files and push status.
    """
    repo_root = git_root()

    # Write files atomically within the project folder
    atomic_write(doc_info.document_md, document_content)
    atomic_write(doc_info.transcript_md, transcript_content)

    if isinstance(speakers_data, dict):
        speakers_json = json.dumps(speakers_data, indent=2)
    else:
        speakers_json = speakers_data

    atomic_write(doc_info.speakers_json, speakers_json)
    if not doc_info.metadata_path().exists():
        metadata = ProjectInfo(
            display_name=doc_info.slug,
            slug=doc_info.slug,
            project_dir=doc_info.project_dir,
            created_at=doc_info.created_at,
        )
        atomic_write(doc_info.metadata_path(), json.dumps(metadata.to_dict(), indent=2))

    # Stage the whole project folder so embedded images and diagram artifacts are
    # committed alongside the markdown — not just a hand-listed subset.
    project_rel = doc_info.project_dir.relative_to(repo_root)
    sha, changed_files = _git_add_and_commit(repo_root, message, [str(project_rel)])

    # Try to push to remote (warn on failure, don't fail the save)
    success, push_message = _git_push(repo_root)

    return GitSaveResult(
        commit_sha=sha,
        committed_files=changed_files,
        push_success=success,
        push_message=push_message,
    )


def save_diagram(
    doc_info: DocumentInfo,
    document_content: str,
    message: str = "Update diagram",
) -> GitSaveResult:
    """Save document (with embedded diagrams), commit to repo, and push to remote.

    Stages the whole project folder so diagram artifacts (`.excalidraw`/SVG/PNG)
    and embedded images are committed, not just the markdown.

    Returns a GitSaveResult with the files actually committed and push status.
    """
    repo_root = git_root()

    atomic_write(doc_info.document_md, document_content)

    project_rel = doc_info.project_dir.relative_to(repo_root)
    sha, changed_files = _git_add_and_commit(repo_root, message, [str(project_rel)])

    # Try to push to remote (warn on failure, don't fail the save)
    success, push_message = _git_push(repo_root)

    return GitSaveResult(
        commit_sha=sha,
        committed_files=changed_files,
        push_success=success,
        push_message=push_message,
    )


def get_history(repo_root: Path | None = None, limit: int = 10) -> list[dict]:
    """Get commit history for the repository. Returns list of {sha, message, timestamp}."""
    repo_root = repo_root or git_root()
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--format=%H%n%s%n%aI"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        commits = []
        lines = result.stdout.strip().split("\n")
        for i in range(0, len(lines), 3):
            if i + 2 < len(lines):
                commits.append({
                    "sha": lines[i],
                    "message": lines[i + 1],
                    "timestamp": lines[i + 2],
                })
        return commits
    except subprocess.CalledProcessError:
        return []
