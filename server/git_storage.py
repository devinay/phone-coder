"""Git-based storage layer for voice-driven documentation.

Replaces versioning with automatic git commits. Each document save is a commit,
and git log provides the complete history.

Structure:
  <docs_root>/
    <slug>/
      .git/                 ← Git repository
      document.md           ← Main document (committed)
      transcript.md         ← Session transcript (committed)
      speakers.json         ← Speaker map (committed)
      diagrams/             ← Diagram artifacts (committed)
      artifacts/            ← Other assets (committed)
      .metadata.json        ← Project metadata (not committed)
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
    """Minimal document metadata (no versioning)."""
    slug: str
    project_dir: Path
    created_at: float
    document_md: Path
    transcript_md: Path
    speakers_json: Path
    diagrams_dir: Path
    artifacts_dir: Path

    def metadata_path(self) -> Path:
        """Path to .metadata.json (project info, not committed to git)."""
        return self.project_dir / ".metadata.json"

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
    """Resolve slug under root and assert it stays inside root."""
    child = (root / slug).resolve()
    if not str(child).startswith(str(root)):
        raise ValueError(f"Path traversal detected: '{slug}' escapes docs root")
    return child


def _git_init(project_dir: Path) -> None:
    """Initialize a git repository in project_dir."""
    try:
        subprocess.run(
            ["git", "init"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "voice-cockpit@local"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Voice Cockpit"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to initialize git repository: {e.stderr.decode()}")


def _git_add_and_commit(project_dir: Path, message: str, paths: list[str] | None = None) -> str:
    """Add and commit files to git. Returns the commit SHA."""
    try:
        # Add all if paths is None, otherwise add specific paths
        if paths is None:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=project_dir,
                capture_output=True,
                check=True,
            )
        else:
            subprocess.run(
                ["git", "add"] + paths,
                cwd=project_dir,
                capture_output=True,
                check=True,
            )

        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )

        # Extract commit SHA from output like "branch/main | create mode 100644 document.md"
        if result.returncode == 0:
            # Get the commit SHA
            sha_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            return sha_result.stdout.strip()
        else:
            # No changes to commit is not an error
            return ""

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git commit failed: {e.stderr}")


def _git_push(repo_dir: Path) -> tuple[bool, str]:
    """Try to push to remote. Returns (success, message).

    Tries to push to 'main' first, then falls back to 'master'.
    On failure, returns (False, error_message) instead of raising.
    """
    for branch in ["main", "master"]:
        try:
            result = subprocess.run(
                ["git", "push", "origin", branch],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, f"Pushed to {branch}"
        except subprocess.TimeoutExpired:
            return False, f"Push to {branch} timed out"
        except Exception as e:
            return False, f"Push to {branch} failed: {str(e)}"

    # Neither branch worked
    return False, "Could not push to main or master (no remote or not configured)"


def create_project(topic_name: str, root: Path | None = None) -> tuple[ProjectInfo, DocumentInfo]:
    """Create a new git-tracked documentation project.

    Returns (ProjectInfo, DocumentInfo) for the newly created project.
    Raises ValueError on path traversal or if slug is empty.
    """
    root = root or docs_root()
    root.mkdir(parents=True, exist_ok=True)

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

    # Initialize git repo
    _git_init(project_dir)

    now = time.time()

    # Create initial document structure
    doc_md = project_dir / f"{slug}.md"
    transcript_md = project_dir / "transcript.md"
    speakers_json = project_dir / "speakers.json"
    diagrams_dir = project_dir / "diagrams"
    artifacts_dir = project_dir / "artifacts"

    diagrams_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Create initial files
    atomic_write(doc_md, f"# {topic_name}\n")
    atomic_write(transcript_md, "")
    atomic_write(speakers_json, "{}")

    # Create .gitignore to exclude local session state
    gitignore_content = """
.session/
.metadata.json.tmp
*.swp
*.swo
*~
.DS_Store
"""
    atomic_write(project_dir / ".gitignore", gitignore_content)

    # Commit initial state
    _git_add_and_commit(project_dir, f"Initial commit: {topic_name}")

    # Write metadata (not committed to git)
    metadata = ProjectInfo(
        display_name=topic_name,
        slug=slug,
        project_dir=project_dir,
        created_at=now,
    )
    metadata_path = project_dir / ".metadata.json"
    atomic_write(metadata_path, json.dumps(metadata.to_dict(), indent=2))

    doc_info = DocumentInfo(
        slug=slug,
        project_dir=project_dir,
        created_at=now,
        document_md=doc_md,
        transcript_md=transcript_md,
        speakers_json=speakers_json,
        diagrams_dir=diagrams_dir,
        artifacts_dir=artifacts_dir,
    )

    return metadata, doc_info


def list_projects(root: Path | None = None) -> list[dict]:
    """Return a list of {slug, display_name} for all git-tracked projects in docs_root."""
    root = root or docs_root()
    if not root.exists():
        return []
    results = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if not (project_dir / ".git").exists():
            continue
        metadata_path = project_dir / ".metadata.json"
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
    """Load an existing git-tracked project by slug. Returns None if not found."""
    root = root or docs_root()
    slug = sanitize_slug(slug)
    project_dir = _safe_child(root, slug)

    if not (project_dir / ".git").exists():
        return None

    metadata_path = project_dir / ".metadata.json"
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

    # Fallback if metadata doesn't exist (shouldn't happen in normal operation)
    return ProjectInfo(
        display_name=slug,
        slug=slug,
        project_dir=project_dir,
        created_at=time.time(),
    )


def load_document(project_dir: Path) -> DocumentInfo | None:
    """Load document info from a git-tracked project. Returns None if not valid."""
    if not (project_dir / ".git").exists():
        return None

    # Infer slug from directory name
    slug = project_dir.name

    # Find the main .md file
    md_candidates = list(project_dir.glob("*.md"))
    if not md_candidates:
        return None
    doc_md = md_candidates[0]

    metadata_path = project_dir / ".metadata.json"
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
        document_md=doc_md,
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
) -> tuple[str, str]:
    """Save document files, commit to git, and push to remote.

    Returns (commit_sha, warning_message). warning_message is empty if push succeeded,
    otherwise contains the error message.
    """
    # Write files atomically
    atomic_write(doc_info.document_md, document_content)
    atomic_write(doc_info.transcript_md, transcript_content)

    if isinstance(speakers_data, dict):
        speakers_json = json.dumps(speakers_data, indent=2)
    else:
        speakers_json = speakers_data

    atomic_write(doc_info.speakers_json, speakers_json)

    # Commit to git
    sha = _git_add_and_commit(
        doc_info.project_dir,
        message,
        ["document.md", "transcript.md", "speakers.json"],
    )

    # Try to push to remote (warn on failure, don't fail the save)
    success, push_message = _git_push(doc_info.project_dir)
    warning = "" if success else f"[WARNING] {push_message}"

    return sha, warning


def save_diagram(
    doc_info: DocumentInfo,
    document_content: str,
    message: str = "Update diagram",
) -> tuple[str, str]:
    """Save document (with embedded diagrams), commit to git, and push to remote.

    Returns (commit_sha, warning_message). warning_message is empty if push succeeded,
    otherwise contains the error message.
    """
    atomic_write(doc_info.document_md, document_content)
    sha = _git_add_and_commit(
        doc_info.project_dir,
        message,
        ["document.md"],
    )

    # Try to push to remote (warn on failure, don't fail the save)
    success, push_message = _git_push(doc_info.project_dir)
    warning = "" if success else f"[WARNING] {push_message}"

    return sha, warning


def get_history(project_dir: Path, limit: int = 10) -> list[dict]:
    """Get commit history for a project. Returns list of {sha, message, timestamp}."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--format=%H%n%s%n%aI"],
            cwd=project_dir,
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
