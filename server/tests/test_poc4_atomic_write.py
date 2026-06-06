"""PoC 4 — Atomic file write and version collision safety.

Run:  uv run pytest tests/test_poc4_atomic_write.py -v
"""

import multiprocessing
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "poc"))
from atomic_write import (
    atomic_write,
    cleanup_stale_tmp,
    create_version_dir,
    sanitize_slug,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _writer_process(path: str, content: str, delay_before_rename: float) -> None:
    """Write to a .tmp file, pause, then rename — used to simulate SIGKILL mid-write."""
    import os
    import tempfile

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    time.sleep(delay_before_rename)
    os.replace(tmp, path)


def _create_version(project_dir: str, results: list) -> None:
    """Worker: create one version dir, record its name."""
    try:
        vdir = create_version_dir(Path(project_dir))
        results.append(vdir.name)
    except Exception as e:
        results.append(f"ERROR: {e}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_write_creates_file(self, tmp_path):
        p = tmp_path / "doc.md"
        atomic_write(p, "hello world")
        assert p.read_text() == "hello world"

    def test_write_bytes(self, tmp_path):
        p = tmp_path / "data.bin"
        atomic_write(p, b"\x00\x01\x02")
        assert p.read_bytes() == b"\x00\x01\x02"

    def test_write_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "c" / "file.txt"
        atomic_write(p, "nested")
        assert p.read_text() == "nested"

    def test_sigkill_leaves_no_corrupt_final_file(self, tmp_path):
        """PASS criterion: SIGKILL mid-rename leaves no corrupt final file."""
        final_path = str(tmp_path / "document.md")
        # Spawn a process that writes but pauses before rename (give us time to kill it)
        proc = multiprocessing.Process(
            target=_writer_process,
            args=(final_path, "corrupt content", 2.0),  # 2s pause before rename
        )
        proc.start()
        time.sleep(0.3)  # wait for .tmp to be written
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=3)

        # Final file must not exist (write was killed before rename)
        assert not Path(final_path).exists(), (
            f"Final file exists after SIGKILL — atomic write not working"
        )

        # .tmp file may or may not exist; cleanup_stale_tmp must handle it
        removed = cleanup_stale_tmp(tmp_path)
        # After cleanup, no .tmp files remain
        remaining_tmp = list(tmp_path.glob("*.tmp"))
        assert remaining_tmp == [], f"Stale .tmp files remain: {remaining_tmp}"

    def test_cleanup_stale_tmp_removes_leftovers(self, tmp_path):
        stale = tmp_path / "foo.tmp"
        stale.write_text("stale")
        removed = cleanup_stale_tmp(tmp_path)
        assert stale in removed
        assert not stale.exists()


class TestVersionCollisionSafety:
    def test_sequential_versions_increment(self, tmp_path):
        v0 = create_version_dir(tmp_path)
        v1 = create_version_dir(tmp_path)
        v2 = create_version_dir(tmp_path)
        assert v0.name == "version_0"
        assert v1.name == "version_1"
        assert v2.name == "version_2"

    def test_concurrent_version_creation_no_collision(self, tmp_path):
        """PASS criterion: two concurrent processes each get a distinct version number."""
        manager = multiprocessing.Manager()
        results = manager.list()

        p1 = multiprocessing.Process(target=_create_version, args=(str(tmp_path), results))
        p2 = multiprocessing.Process(target=_create_version, args=(str(tmp_path), results))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        names = list(results)
        assert len(names) == 2, f"Expected 2 results, got: {names}"
        errors = [r for r in names if r.startswith("ERROR")]
        assert not errors, f"Worker errors: {errors}"

        # Exactly one version_0 and one version_1 (or both get 0 and 1 in some order)
        assert sorted(names) == ["version_0", "version_1"], (
            f"Unexpected version names: {sorted(names)}"
        )

        # Directories exist on disk
        assert (tmp_path / "version_0").is_dir()
        assert (tmp_path / "version_1").is_dir()


class TestSlugSanitization:
    @pytest.mark.parametrize("raw,expected", [
        ("My Project", "my-project"),          # space → dash
        ("../../etc/passwd", "etc-passwd"),    # dots and slashes → dash, collapse
        ("hello/world", "hello-world"),        # slash → dash
        ("  spaces  ", "spaces"),              # leading/trailing stripped
        ("已知", "untitled"),                   # non-ASCII stripped → empty → default
        ("", "untitled"),                       # empty → default
        ("--leading-dashes--", "leading-dashes"),
        ("rm -rf *", "rm-rf"),                 # space→dash, metachar stripped, collapsed
    ])
    def test_sanitize(self, raw, expected):
        result = sanitize_slug(raw)
        assert result == expected, f"sanitize_slug({raw!r}) = {result!r}, expected {expected!r}"

    def test_path_traversal_stays_in_root(self, tmp_path):
        """Sanitized slug must not escape docs root."""
        docs_root = tmp_path / "docs"
        docs_root.mkdir()
        slug = sanitize_slug("../../etc/passwd")
        target = (docs_root / slug).resolve()
        assert str(target).startswith(str(docs_root.resolve())), (
            f"Path traversal possible: {target}"
        )
