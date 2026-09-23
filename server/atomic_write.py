"""Atomic file-write utilities for the documentation storage layer.

All writes use write-then-rename to prevent partial files.
Version directories are created with a project-level lock to prevent collisions.
"""

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write(path: Path, content: str | bytes, encoding: str = "utf-8") -> None:
    """Write content atomically via a temp file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": encoding}
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, mode, **kwargs) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def cleanup_stale_tmp(directory: Path) -> list[Path]:
    """Remove any .tmp files left by a previously killed process."""
    removed = []
    for p in Path(directory).glob("*.tmp"):
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            pass
    return removed


class ProjectLock:
    """POSIX advisory lock scoped to a project directory."""

    def __init__(self, project_dir: Path) -> None:
        self._lock_path = Path(project_dir) / ".project.lock"
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None

    def acquire(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        self._fd = open(self._lock_path, "w")
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > deadline:
                    self._fd.close()
                    self._fd = None
                    raise TimeoutError(f"Could not acquire project lock within {timeout}s")
                time.sleep(0.05)

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


def next_version_number(project_dir: Path) -> int:
    """Return the next available version number under project_dir (with lock held)."""
    n = 0
    while (project_dir / f"version_{n}").exists():
        n += 1
    return n


def create_version_dir(project_dir: Path) -> Path:
    """Atomically create the next version_N directory inside project_dir.

    Uses a project-level lock so concurrent callers get distinct version numbers.
    """
    project_dir = Path(project_dir)
    with ProjectLock(project_dir):
        n = next_version_number(project_dir)
        version_dir = project_dir / f"version_{n}"
        version_dir.mkdir(parents=True, exist_ok=False)
        return version_dir


def sanitize_slug(name: str) -> str:
    """Convert an arbitrary user-supplied name into a safe directory slug.

    Strips path-traversal characters and shell metacharacters.
    """
    import re
    slug = name.lower().strip()
    # Convert whitespace and path/word separators to dashes
    slug = re.sub(r"[\s/\\.]", "-", slug)
    # Strip shell metacharacters and all other unsafe chars
    slug = re.sub(r"[^a-z0-9\-_]", "", slug)
    # Collapse multiple dashes
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    slug = slug[:30].rstrip("-")
    return slug or "untitled"


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, indent=2))


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())
