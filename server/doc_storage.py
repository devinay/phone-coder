"""Phase 1 — Storage layer for voice-driven documentation.

Creates and manages the versioned directory structure under VOICE_COCKPIT_DOCS_ROOT:

  <docs_root>/
    <slug>/
      project.json
      version_0/
        manifest.json
        document.md
        transcript.md
        speakers.json
        diagrams/
        artifacts/
        .session/        ← autosave; never committed as a version
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from poc.atomic_write import (
    ProjectLock,
    atomic_write,
    cleanup_stale_tmp,
    create_version_dir,
    next_version_number,
    sanitize_slug,
    write_json,
)


# ── Public re-exports so callers only import from doc_storage ────────────────
__all__ = [
    "sanitize_slug",
    "atomic_write",
    "create_version_dir",
    "cleanup_stale_tmp",
    "ProjectInfo",
    "VersionInfo",
    "create_project",
    "load_project",
    "create_next_version",
    "load_version",
    "docs_root",
]


def docs_root() -> Path:
    """Return the configured docs root, expanding ~ and env vars."""
    raw = os.getenv("VOICE_COCKPIT_DOCS_ROOT", "~/voice-cockpit-docs")
    return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()


def _safe_child(root: Path, slug: str) -> Path:
    """Resolve slug under root and assert it stays inside root."""
    child = (root / slug).resolve()
    if not str(child).startswith(str(root)):
        raise ValueError(f"Path traversal detected: '{slug}' escapes docs root")
    return child


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class VersionInfo:
    version: int
    version_dir: Path
    derived_from: int | None
    created_at: float
    document_md: Path
    transcript_md: Path
    speakers_json: Path
    diagrams_dir: Path
    artifacts_dir: Path
    session_dir: Path

    def manifest_path(self) -> Path:
        return self.version_dir / "manifest.json"

    def to_manifest(self) -> dict:
        rel = lambda p: str(p.relative_to(self.version_dir.parent))  # relative to project dir
        return {
            "version": self.version,
            "derived_from": self.derived_from,
            "created_at": self.created_at,
            "document_md": rel(self.document_md),
            "transcript_md": rel(self.transcript_md),
            "speakers_json": rel(self.speakers_json),
            "diagrams_dir": rel(self.diagrams_dir),
            "artifacts_dir": rel(self.artifacts_dir),
        }


@dataclass
class ProjectInfo:
    display_name: str
    slug: str
    project_dir: Path
    created_at: float
    current_version: int
    versions: list[int] = field(default_factory=list)

    def project_json_path(self) -> Path:
        return self.project_dir / "project.json"

    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "slug": self.slug,
            "created_at": self.created_at,
            "current_version": self.current_version,
            "versions": self.versions,
        }


# ── Version helpers ───────────────────────────────────────────────────────────

def _init_version_dir(project_dir: Path, version_n: int, derived_from: int | None) -> VersionInfo:
    """Populate a freshly created version_N directory with all required files."""
    vdir = project_dir / f"version_{version_n}"
    now = time.time()

    doc_md = vdir / "document.md"
    transcript_md = vdir / "transcript.md"
    speakers_json = vdir / "speakers.json"
    diagrams_dir = vdir / "diagrams"
    artifacts_dir = vdir / "artifacts"
    session_dir = vdir / ".session"

    for d in (diagrams_dir, artifacts_dir, session_dir):
        d.mkdir(parents=True, exist_ok=True)

    atomic_write(doc_md, "")
    atomic_write(transcript_md, "")
    write_json(speakers_json, {})

    info = VersionInfo(
        version=version_n,
        version_dir=vdir,
        derived_from=derived_from,
        created_at=now,
        document_md=doc_md,
        transcript_md=transcript_md,
        speakers_json=speakers_json,
        diagrams_dir=diagrams_dir,
        artifacts_dir=artifacts_dir,
        session_dir=session_dir,
    )
    write_json(info.manifest_path(), info.to_manifest())
    return info


# ── Public API ────────────────────────────────────────────────────────────────

def create_project(topic_name: str, root: Path | None = None) -> tuple[ProjectInfo, VersionInfo]:
    """Create a new project directory with version_0.

    Returns (ProjectInfo, VersionInfo) for the newly created project.
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

    now = time.time()
    # Create version_0 with project lock held to prevent races
    with ProjectLock(project_dir):
        _init_version_dir(project_dir, 0, derived_from=None)

    version_dir = project_dir / "version_0"
    doc_md = version_dir / "document.md"
    transcript_md = version_dir / "transcript.md"
    speakers_json = version_dir / "speakers.json"

    version_info = VersionInfo(
        version=0,
        version_dir=version_dir,
        derived_from=None,
        created_at=now,
        document_md=doc_md,
        transcript_md=transcript_md,
        speakers_json=speakers_json,
        diagrams_dir=version_dir / "diagrams",
        artifacts_dir=version_dir / "artifacts",
        session_dir=version_dir / ".session",
    )

    project_info = ProjectInfo(
        display_name=topic_name,
        slug=slug,
        project_dir=project_dir,
        created_at=now,
        current_version=0,
        versions=[0],
    )
    write_json(project_info.project_json_path(), project_info.to_dict())

    return project_info, version_info


def load_project(slug: str, root: Path | None = None) -> ProjectInfo | None:
    """Load an existing project by slug. Returns None if not found."""
    root = root or docs_root()
    slug = sanitize_slug(slug)
    project_dir = _safe_child(root, slug)
    pjson = project_dir / "project.json"
    if not pjson.exists():
        return None
    data = json.loads(pjson.read_text())
    return ProjectInfo(
        display_name=data["display_name"],
        slug=data["slug"],
        project_dir=project_dir,
        created_at=data["created_at"],
        current_version=data["current_version"],
        versions=data.get("versions", []),
    )


def load_version(project_dir: Path, version: int) -> VersionInfo | None:
    """Load a VersionInfo from an existing version_N directory."""
    vdir = project_dir / f"version_{version}"
    manifest_path = vdir / "manifest.json"
    if not manifest_path.exists():
        return None
    data = json.loads(manifest_path.read_text())
    return VersionInfo(
        version=data["version"],
        version_dir=vdir,
        derived_from=data.get("derived_from"),
        created_at=data["created_at"],
        document_md=project_dir / data["document_md"],
        transcript_md=project_dir / data["transcript_md"],
        speakers_json=project_dir / data["speakers_json"],
        diagrams_dir=project_dir / data["diagrams_dir"],
        artifacts_dir=project_dir / data["artifacts_dir"],
        session_dir=vdir / ".session",
    )


def create_next_version(project_dir: Path, derived_from: int) -> VersionInfo:
    """Create the next version_N directory (for review mode or new iteration)."""
    with ProjectLock(project_dir):
        n = next_version_number(project_dir)
        vdir = project_dir / f"version_{n}"
        vdir.mkdir(parents=True, exist_ok=False)
        return _init_version_dir(project_dir, n, derived_from=derived_from)


def autosave_session(version_info: VersionInfo, state: dict) -> None:
    """Write session autosave state to .session/autosave.json (not a committed version)."""
    path = version_info.session_dir / "autosave.json"
    write_json(path, state)


def load_autosave(version_info: VersionInfo) -> dict | None:
    """Load session autosave state. Returns None if not found."""
    path = version_info.session_dir / "autosave.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
