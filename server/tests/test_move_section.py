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
