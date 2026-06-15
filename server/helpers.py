"""Utility functions for markdown, mermaid, and TTS processing."""

import re

_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _find_section(lines: list[str], section: str) -> tuple[int | None, int]:
    """Find a markdown header (## .. ######) whose title equals `section`.

    Returns (start_index, level). start_index is None if not found.
    """
    target = section.strip()
    for i, l in enumerate(lines):
        m = _MD_HEADER_RE.match(l)
        if m and len(m.group(1)) >= 2 and m.group(2).strip() == target:
            return i, len(m.group(1))
    return None, 2


def _section_end(lines: list[str], start: int, level: int) -> int | None:
    """Index of the next header at the same or shallower level after `start`, or None."""
    for i in range(start + 1, len(lines)):
        m = _MD_HEADER_RE.match(lines[i])
        if m and len(m.group(1)) <= level:
            return i
    return None


def _replace_section(doc: str, section: str, new_content: str) -> str:
    """Replace the body under a markdown header named `section` (any level ##–######),
    or append a new ## section if none exists. Matching by name (not level) means an
    existing ### subsection is edited in place instead of spawning a duplicate ## header.
    """
    lines = doc.splitlines(keepends=True)
    start, level = _find_section(lines, section)
    if start is None:
        header = f"## {section}"
        sep = "\n\n" if doc and not doc.endswith("\n\n") else ""
        return doc + sep + header + "\n\n" + new_content.strip() + "\n"
    end = _section_end(lines, start, level)
    before = "".join(lines[: start + 1])
    after = "".join(lines[end:]) if end is not None else ""
    return before + "\n\n" + new_content.strip() + "\n\n" + after


def _strip_generated_sections(doc: str) -> str:
    """Remove a previously generated ``## Summary`` block and trailing Transcript
    ``<details>`` so re-saving an opened document replaces them instead of stacking
    duplicates. Content the user authored in between is left untouched.
    """
    # Trailing transcript collapsible (plus any --- separator that precedes it).
    doc = re.sub(
        r"\n*(?:---[ \t]*\n+)?<details>\s*<summary>Transcript</summary>.*?</details>\s*$",
        "\n",
        doc,
        flags=re.DOTALL,
    )
    # Leading "## Summary ... \n---\n" block (only the first occurrence).
    doc = re.sub(
        r"## Summary\n.*?\n---[ \t]*\n+",
        "",
        doc,
        count=1,
        flags=re.DOTALL,
    )
    return doc.rstrip() + "\n"


# ── Mermaid diagram helpers ───────────────────────────────────────────────────

# Diagram types the Mermaid CDN version supports reliably in production
MERMAID_SUPPORTED_TYPES = {
    "flowchart", "graph", "sequenceDiagram", "classDiagram",
    "stateDiagram", "stateDiagram-v2", "erDiagram", "gantt",
    "pie", "gitGraph", "journey", "mindmap", "timeline",
    "block-beta", "quadrantChart",
}

# Types that are beta/unstable — agent must re-prompt with a supported fallback
MERMAID_UNSUPPORTED_TYPES = {
    "xychart-beta", "sankey-beta", "C4Context", "C4Container", "C4Component",
}

# Pattern that matches a diagram block in document.md:
#   <!-- diagram-id: <id> -->
#   ```mermaid
#   <source>
#   ```
_DIAGRAM_BLOCK_RE = re.compile(
    r'(<!-- diagram-id: (?P<id>[a-z0-9_-]+) -->\n```mermaid\n)(?P<src>.*?)```',
    re.DOTALL,
)

# Pattern that matches a placeholder inserted by the agent in Phase 1:
#   <!-- diagram: <description> -->
_DIAGRAM_PLACEHOLDER_RE = re.compile(r'<!-- diagram: (?P<desc>[^>]+?) -->')


def _validate_mermaid_source(source: str, diagram_type: str) -> tuple[bool, str]:
    """Check that source starts with the declared diagram type keyword.

    Returns (is_valid, error_message).
    """
    stripped = source.strip()
    valid_starts = {diagram_type}
    if diagram_type == "flowchart":
        valid_starts.add("graph")
    for start in valid_starts:
        if stripped.lower().startswith(start.lower()):
            return True, ""
    first_line = stripped.splitlines()[0] if stripped else "(empty)"
    return False, (
        f"Source first line '{first_line}' does not match declared type '{diagram_type}'. "
        f"Mermaid source must begin with the diagram keyword."
    )


def _build_diagram_block(diagram_id: str, mermaid_source: str) -> str:
    return f"<!-- diagram-id: {diagram_id} -->\n```mermaid\n{mermaid_source.strip()}\n```"


def _insert_diagram_in_doc(
    doc: str, diagram_id: str, mermaid_source: str, replace_placeholder: str | None
) -> str:
    """Insert a new diagram block, optionally replacing a placeholder comment."""
    block = _build_diagram_block(diagram_id, mermaid_source)
    if replace_placeholder:
        # Try exact text match first, then partial match
        placeholder = f"<!-- diagram: {replace_placeholder} -->"
        if placeholder in doc:
            return doc.replace(placeholder, block, 1)
        # Partial match: find any placeholder whose description contains the text
        m = _DIAGRAM_PLACEHOLDER_RE.search(doc)
        if m:
            return doc[:m.start()] + block + doc[m.end():]
    # No placeholder: append before the last newline or at end
    sep = "\n\n" if doc and not doc.endswith("\n\n") else ""
    return doc + sep + block + "\n"


def _update_diagram_in_doc(doc: str, diagram_id: str, mermaid_source: str) -> tuple[str, bool]:
    """Replace the mermaid source inside an existing diagram block.

    Returns (new_doc, found).
    """
    block = _build_diagram_block(diagram_id, mermaid_source)

    new_doc, count = _DIAGRAM_BLOCK_RE.subn(
        lambda m: block if m.group("id") == diagram_id else m.group(0),
        doc,
    )
    return new_doc, count > 0


def _extract_diagram_source(doc: str, diagram_id: str) -> str | None:
    """Extract the mermaid source for a diagram block by ID. Returns None if not found."""
    for m in _DIAGRAM_BLOCK_RE.finditer(doc):
        if m.group("id") == diagram_id:
            return m.group("src").strip()
    return None


def _move_diagram_in_doc(doc: str, diagram_id: str, target_section: str) -> tuple[str, str]:
    """Relocate the (single, marked) diagram block to the end of `target_section`'s body.

    Removes the block from its current position and re-inserts it — preserving the
    ``<!-- diagram-id -->`` marker — so the diagram never ends up duplicated.

    Returns (new_doc, status) where status is one of:
      "ok"           — moved successfully
      "no_diagram"   — no block with that id
      "no_section"   — target section header not found (doc unchanged)
    """
    match = next((m for m in _DIAGRAM_BLOCK_RE.finditer(doc) if m.group("id") == diagram_id), None)
    if match is None:
        return doc, "no_diagram"
    block = doc[match.start():match.end()].strip()

    # Remove the block from its current location, collapsing the surrounding blank lines.
    without = (doc[:match.start()].rstrip() + "\n\n" + doc[match.end():].lstrip()).strip() + "\n"

    lines = without.splitlines(keepends=True)
    start, level = _find_section(lines, target_section)
    if start is None:
        return doc, "no_section"  # leave the original untouched

    end = _section_end(lines, start, level)
    end_idx = end if end is not None else len(lines)
    before = "".join(lines[:end_idx]).rstrip()
    after = "".join(lines[end_idx:]).lstrip()
    new_doc = before + "\n\n" + block + "\n\n" + after
    return new_doc.rstrip() + "\n", "ok"


def _split_for_tts(text: str, max_len: int) -> list[str]:
    """Split text into chunks no longer than max_len, preferring sentence then word
    boundaries. Keeps each chunk well under Kokoro's phoneme cap.
    """
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else []

    # First split on sentence boundaries, then pack greedily up to max_len.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        while len(s) > max_len:
            # A single over-long sentence: break on the last space before max_len.
            cut = s.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= max_len:
            cur = f"{cur} {s}"
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]
