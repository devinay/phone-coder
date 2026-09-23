"""Tests for section reordering.

Reordering was previously only possible through edit_doc, which needs one exact
match spanning everything between the old and new positions. Models cannot
reproduce a span that long verbatim, so the match failed, nothing moved, and the
reply said it was done — a silent no-op reported as success.
"""

from helpers import _move_section

DOC = """# My Notes

Some preamble.

## Alpha
first

## Beta
second

## Gamma
third
"""


def sections(doc: str) -> list[str]:
    return [ln[3:].strip() for ln in doc.splitlines() if ln.startswith("## ")]


class TestMoveToTop:
    def test_a_section_moves_above_the_others(self):
        out, err = _move_section(DOC, "Gamma", to_top=True)
        assert not err
        assert sections(out) == ["Gamma", "Alpha", "Beta"]

    def test_the_document_title_is_not_displaced(self):
        """"Top" never means above the thing that names the document."""
        out, _ = _move_section(DOC, "Gamma", to_top=True)
        assert out.splitlines()[0] == "# My Notes"
        assert "Some preamble." in out.split("## Gamma")[0]

    def test_the_body_travels_with_the_heading(self):
        out, _ = _move_section(DOC, "Gamma", to_top=True)
        assert out.split("## Gamma")[1].lstrip().startswith("third")

    def test_moving_is_idempotent(self):
        once, _ = _move_section(DOC, "Gamma", to_top=True)
        twice, _ = _move_section(once, "Gamma", to_top=True)
        assert once == twice


class TestMoveBefore:
    def test_a_section_moves_above_a_named_one(self):
        out, err = _move_section(DOC, "Alpha", before="Gamma")
        assert not err
        assert sections(out) == ["Beta", "Alpha", "Gamma"]

    def test_an_unknown_target_is_refused_without_changing_anything(self):
        out, err = _move_section(DOC, "Alpha", before="Nowhere")
        assert out == DOC
        assert "Nowhere" in err


class TestFailuresAreReported:
    """The point of the tool: a failed move must not look like a success."""

    def test_an_unknown_section_is_named(self):
        out, err = _move_section(DOC, "Missing", to_top=True)
        assert out == DOC
        assert "Missing" in err

    def test_sections_do_not_weld_together(self):
        """A section taken from the end has no trailing blank line."""
        out, _ = _move_section(DOC, "Gamma", to_top=True)
        assert "third\n\n## Alpha" in out

    def test_moving_to_the_end_works(self):
        out, err = _move_section(DOC, "Alpha")
        assert not err
        assert sections(out) == ["Beta", "Gamma", "Alpha"]


class TestSubsections:
    def test_a_subsection_takes_its_own_body_only(self):
        doc = (
            "# T\n\n## One\nbody one\n\n### Nested\nnested body\n\n## Two\nbody two\n"
        )
        out, err = _move_section(doc, "Nested", before="One")
        assert not err
        assert out.index("### Nested") < out.index("## One")
        assert "body two" in out
        # The parent keeps its own body, which came before the subsection.
        assert "body one" in out


class TestHeadingMatching:
    """Exact string matching is why documents grew duplicate sections.

    Asking to update "overview" when the document said "## Overview" found
    nothing and appended a second section meaning the same thing. Spoken input
    makes this the normal case: nobody dictates capitalisation, and the
    recogniser adds trailing punctuation freely.
    """

    import pytest

    @pytest.mark.parametrize(
        "asked", ["Overview", "overview", "OVERVIEW", "Overview:", " overview. "]
    )
    def test_a_section_is_found_however_it_is_worded(self, asked):
        from helpers import _find_section

        doc = "# T\n\n## Overview\nbody\n\n## Other\nx\n"
        assert _find_section(doc.splitlines(keepends=True), asked)[0] is not None

    def test_writing_edits_in_place_rather_than_duplicating(self):
        from helpers import _replace_section

        doc = "# T\n\n## Overview\noriginal\n\n## Other\nx\n"
        out = _replace_section(doc, "overview", "replaced")
        assert out.count("## Overview") == 1
        assert "## overview" not in out
        assert "replaced" in out and "original" not in out

    def test_a_genuinely_new_section_is_still_created(self):
        from helpers import _replace_section

        doc = "# T\n\n## Overview\nbody\n"
        assert "## Risks" in _replace_section(doc, "Risks", "new")

    def test_differently_worded_headings_stay_separate(self):
        """Normalising case must not merge sections that mean different things."""
        from helpers import _find_section

        doc = "# T\n\n## Summary\na\n\n## Overview\nb\n"
        i, _ = _find_section(doc.splitlines(keepends=True), "Summary")
        assert doc.splitlines()[i].strip() == "## Summary"


class TestDuplicateReporting:
    def test_duplicates_are_detected_across_casing(self):
        from helpers import duplicate_sections

        doc = "# T\n\n## Notes\na\n\n## Detail\nd\n\n## notes\nb\n"
        assert duplicate_sections(doc) == {"Notes": 2}

    def test_a_clean_document_reports_none(self):
        from helpers import duplicate_sections

        assert duplicate_sections("# T\n\n## A\na\n\n## B\nb\n") == {}

    def test_the_document_title_is_not_counted(self):
        """Only ## and deeper are sections; an H1 is the document's name."""
        from helpers import duplicate_sections

        assert duplicate_sections("# T\n\n## A\na\n") == {}


class TestAmbiguousMovesAreRefused:
    """Moving the first of several would quietly pick the wrong one."""

    DUP = "# T\n\n## Notes\none\n\n## Detail\nd\n\n## notes\ntwo\n"

    def test_an_ambiguous_source_is_refused(self):
        out, err = _move_section(self.DUP, "Notes", to_top=True)
        assert out == self.DUP
        assert "appears 2 times" in err

    def test_an_ambiguous_destination_is_refused(self):
        out, err = _move_section(self.DUP, "Detail", before="notes")
        assert out == self.DUP
        assert "ambiguous" in err

    def test_a_unique_section_still_moves(self):
        out, err = _move_section(self.DUP, "Detail", to_top=True)
        assert not err
        assert out.index("## Detail") < out.index("## Notes")


class TestMergeSections:
    """The way out of a duplicate, since moves now refuse to guess.

    Nothing is discarded: two sections sharing a name may hold different
    content, and choosing which survives is the user's decision, not one this
    can make for them.
    """

    DUP = "# T\n\n## Notes\nfirst body\n\n## Detail\nd\n\n## notes\nsecond body\n"

    def _merged(self, section="Notes"):
        from helpers import _merge_sections

        return _merge_sections(self.DUP, section)

    def test_no_content_is_lost(self):
        out, err = self._merged()
        assert not err
        assert "first body" in out and "second body" in out

    def test_the_duplicate_is_gone(self):
        from helpers import duplicate_sections

        out, _ = self._merged()
        assert duplicate_sections(out) == {}
        assert out.lower().count("## notes") == 1

    def test_bodies_keep_document_order(self):
        out, _ = self._merged()
        assert out.index("first body") < out.index("second body")

    def test_unrelated_sections_survive(self):
        out, _ = self._merged()
        assert "## Detail" in out and "d" in out

    def test_the_first_headings_wording_is_kept(self):
        """It was there first; the later one is the interloper."""
        out, _ = self._merged()
        assert "## Notes" in out

    def test_merging_a_unique_section_is_refused_not_silently_done(self):
        _, err = self._merged("Detail")
        assert "only once" in err

    def test_merging_a_missing_section_is_refused(self):
        _, err = self._merged("Nowhere")
        assert "No section" in err

    def test_a_merged_section_can_then_be_moved(self):
        """The whole point: merging unblocks the move that was refused."""
        from helpers import _merge_sections

        merged, _ = _merge_sections(self.DUP, "Notes")
        out, err = _move_section(merged, "Notes", to_top=True)
        assert not err
        assert out.index("## Notes") < out.index("## Detail")
