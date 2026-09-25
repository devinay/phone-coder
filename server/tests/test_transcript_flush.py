"""The transcript must survive a crash, not only a clean exit.

It was accumulated in memory and written only by exit_doc_mode, so a crash, a
dropped connection, or simply never saying "exit doc mode" lost every utterance
of the session. The user's transcript.md was 0 bytes after eleven sessions.
"""

import json

from doc_writer import AttributedUtterance, DocWriter
from git_storage import flush_transcript


class FakeDocInfo:
    def __init__(self, tmp_path):
        self.transcript_md = tmp_path / "transcript.md"
        self.speakers_json = tmp_path / "speakers.json"


def writer_with(n=2):
    w = DocWriter(title="Session")
    for i in range(n):
        w.add_utterance(
            AttributedUtterance(
                text=f"utterance {i}", timestamp=1790000000.0 + i,
                speaker_id="user" if i % 2 == 0 else "controller", confidence=None,
            )
        )
    return w


class TestFlush:
    def test_it_writes_the_transcript_without_a_commit(self, tmp_path):
        info = FakeDocInfo(tmp_path)
        assert flush_transcript(info, writer_with().render_transcript_md(), {})
        assert "utterance 0" in info.transcript_md.read_text()
        assert "utterance 1" in info.transcript_md.read_text()

    def test_it_writes_the_speaker_map(self, tmp_path):
        info = FakeDocInfo(tmp_path)
        flush_transcript(info, "x", {"user": "Vinay"})
        assert json.loads(info.speakers_json.read_text()) == {"user": "Vinay"}

    def test_a_string_speaker_map_is_passed_through(self, tmp_path):
        info = FakeDocInfo(tmp_path)
        flush_transcript(info, "x", '{"already": "json"}')
        assert json.loads(info.speakers_json.read_text()) == {"already": "json"}

    def test_flushing_repeatedly_replaces_rather_than_appends(self, tmp_path):
        """Called every turn, so it must not accumulate copies."""
        info = FakeDocInfo(tmp_path)
        for n in (1, 2, 3):
            flush_transcript(info, writer_with(n).render_transcript_md(), {})
        assert info.transcript_md.read_text().count("utterance 0") == 1

    def test_a_missing_directory_is_created_rather_than_failing(self, tmp_path):
        """atomic_write makes parents, so a fresh project folder is not a failure."""
        info = FakeDocInfo(tmp_path / "new" / "project")
        assert flush_transcript(info, "x", {}) is True
        assert info.transcript_md.read_text() == "x"

    def test_a_genuine_write_failure_is_reported_not_raised(self, tmp_path):
        """A failed flush costs one turn of transcript; raising would cost the reply."""
        blocked = tmp_path / "blocked"
        blocked.write_text("I am a file, not a directory")
        info = FakeDocInfo(blocked / "under-a-file")
        assert flush_transcript(info, "x", {}) is False

    def test_the_growing_transcript_is_always_complete(self, tmp_path):
        """Each flush writes the whole transcript, so the last one is sufficient."""
        info = FakeDocInfo(tmp_path)
        flush_transcript(info, writer_with(5).render_transcript_md(), {})
        text = info.transcript_md.read_text()
        assert all(f"utterance {i}" in text for i in range(5))
