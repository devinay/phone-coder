"""Phase 1 — Milestone 1.3: DocWriter and AttributedUtterance tests."""

import time

import pytest

from doc_writer import AttributedUtterance, DocWriter


def make_utterance(text, speaker_id=None, confidence=None):
    return AttributedUtterance(
        text=text,
        timestamp=time.time(),
        speaker_id=speaker_id,
        confidence=confidence,
    )


# ── AttributedUtterance.display_name ─────────────────────────────────────────

def test_display_name_with_map():
    u = make_utterance("Hello", speaker_id="0")
    assert u.display_name({"0": "Alice"}) == "Alice"

def test_display_name_unknown_speaker_id():
    u = make_utterance("Hello", speaker_id="3")
    assert u.display_name({"0": "Alice"}) == "Speaker 3"

def test_display_name_no_speaker_id():
    u = make_utterance("Hello", speaker_id=None)
    assert u.display_name({}) == "Speaker Unknown"

def test_display_name_custom_fallback():
    u = AttributedUtterance(
        text="Hi",
        timestamp=time.time(),
        speaker_id=None,
        confidence=None,
        fallback_label="Unidentified",
    )
    assert u.display_name({}) == "Unidentified"


# ── DocWriter — empty session ─────────────────────────────────────────────────

def test_empty_session_document_md():
    dw = DocWriter(title="Test")
    doc = dw.render_document_md()
    assert "## Main Content" in doc
    assert "## Transcript" in doc

def test_empty_session_transcript_md():
    dw = DocWriter()
    transcript = dw.render_transcript_md()
    assert "No utterances" in transcript

def test_utterance_count_zero():
    dw = DocWriter()
    assert dw.utterance_count() == 0


# ── DocWriter — speaker attribution ─────────────────────────────────────────

def test_two_speaker_grouping():
    dw = DocWriter(title="Meeting")
    dw.set_speaker_map({"0": "Alice", "1": "Bob"})
    dw.add_utterance(make_utterance("Alice says hi", speaker_id="0"))
    dw.add_utterance(make_utterance("Bob replies", speaker_id="1"))
    dw.add_utterance(make_utterance("Alice continues", speaker_id="0"))

    doc = dw.render_document_md()
    assert "### Alice" in doc
    assert "### Bob" in doc
    # Alice's utterances are in Alice's section, not Bob's
    alice_section = doc.split("### Alice")[1].split("### Bob")[0]
    assert "Alice says hi" in alice_section
    assert "Alice continues" in alice_section
    assert "Bob replies" not in alice_section

def test_bob_utterance_in_bob_section():
    dw = DocWriter(title="Meeting")
    dw.set_speaker_map({"0": "Alice", "1": "Bob"})
    dw.add_utterance(make_utterance("Alice speaks", speaker_id="0"))
    dw.add_utterance(make_utterance("Bob speaks", speaker_id="1"))

    doc = dw.render_document_md()
    bob_section = doc.split("### Bob")[1]
    assert "Bob speaks" in bob_section
    assert "Alice speaks" not in bob_section

def test_four_utterance_two_speaker_exchange():
    dw = DocWriter(title="Meeting")
    dw.set_speaker_map({"0": "Alice", "1": "Bob"})
    for i in range(2):
        dw.add_utterance(make_utterance(f"Alice utterance {i}", speaker_id="0"))
        dw.add_utterance(make_utterance(f"Bob utterance {i}", speaker_id="1"))

    doc = dw.render_document_md()
    assert "## Main Content" in doc
    assert "## Transcript" in doc
    assert "### Alice" in doc
    assert "### Bob" in doc
    assert dw.utterance_count() == 4


# ── DocWriter — missing speaker field ────────────────────────────────────────

def test_missing_speaker_falls_back_to_unknown():
    dw = DocWriter()
    dw.add_utterance(make_utterance("anonymous", speaker_id=None))

    doc = dw.render_document_md()
    assert "### Speaker Unknown" in doc
    assert "anonymous" in doc

def test_missing_speaker_utterances_not_dropped():
    dw = DocWriter()
    dw.add_utterance(make_utterance("line one", speaker_id=None))
    dw.add_utterance(make_utterance("line two", speaker_id=None))

    doc = dw.render_document_md()
    assert "line one" in doc
    assert "line two" in doc


# ── DocWriter — transcript.md ─────────────────────────────────────────────────

def test_transcript_md_includes_timestamp():
    dw = DocWriter()
    dw.set_speaker_map({"0": "Alice"})
    dw.add_utterance(make_utterance("hello", speaker_id="0"))
    transcript = dw.render_transcript_md()
    # Format: [HH:MM:SS]
    import re
    assert re.search(r"\[\d{2}:\d{2}:\d{2}\]", transcript)

def test_transcript_md_includes_speaker_name():
    dw = DocWriter()
    dw.set_speaker_map({"0": "Alice"})
    dw.add_utterance(make_utterance("test utterance", speaker_id="0"))
    transcript = dw.render_transcript_md()
    assert "Alice" in transcript
    assert "test utterance" in transcript
