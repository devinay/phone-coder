"""Utility functions for markdown, mermaid, and TTS processing."""

import re

from loguru import logger

_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def message_dict(message) -> dict | None:
    """A context message as a plain dict, or None if it is not one.

    Context messages are not uniformly dicts. Pipecat wraps provider-specific
    entries in ``LLMSpecificMessage`` (a dataclass with ``.llm`` and
    ``.message``), which the Anthropic service puts into the context — so every
    ``message.get(...)`` in this codebase was an exception waiting for someone
    to run a non-OpenAI model. That happened the first time Anthropic was
    selected, and it broke every turn before the LLM was even called.

    Unwraps the container and returns None for anything still not dict-like,
    so callers can skip rather than crash. Inspecting the conversation is
    always optional work; failing it must never take the turn down.
    """
    inner = getattr(message, "message", message)
    return inner if isinstance(inner, dict) else None


def guard_context(context) -> None:
    """Stop unrenderable messages entering the context at all.

    Filtering by frame does not work here. The assistant aggregator sits at the
    end of the pipeline, so the context frame it pushes after a tool result
    never traverses a processor placed before the LLM — the guard simply never
    ran on the path where it was needed.

    The context itself is the one place every message must pass through,
    whoever adds it and wherever they sit. So ``add_message`` is wrapped: a
    message the provider cannot render is dropped at the door, logged, and
    never gets the chance to kill a later turn.
    """
    def refuse(messages, how: str):
        kept = [m for m in messages if is_renderable_message(m)]
        if len(kept) != len(messages):
            logger.warning(
                f"[CONTEXT] refused {len(messages) - len(kept)} message(s) via {how} "
                "that no provider can render (a reasoning artifact without a role)"
            )
        return kept

    add_one, add_many, set_all = (
        context.add_message,
        getattr(context, "add_messages", None),
        getattr(context, "set_messages", None),
    )

    def add_message(message):
        return add_one(message) if is_renderable_message(message) else refuse([message], "add") or None

    context.add_message = add_message
    # Every entry point, because one unguarded route is the whole bug: the
    # frame-based guard covered the user turn and missed the tool result.
    if add_many is not None:
        context.add_messages = lambda messages: add_many(refuse(messages, "add_messages"))
    if set_all is not None:
        context.set_messages = lambda messages: set_all(refuse(messages, "set_messages"))


def is_renderable_message(message) -> bool:
    """Whether a provider can actually turn this message into a request.

    Pipecat's Anthropic adapter passes provider-specific messages through
    verbatim and then reads ``message["role"]``, so any entry without a role
    raises ``KeyError('role')`` and fails the whole turn — with an error that
    names neither the message nor where it came from.

    Anthropic's own reasoning artifacts can take that shape: a thought without
    a signature misses the adapter's thought branch and falls through raw. So a
    turn can be killed by a message the model itself produced.

    Dicts are trusted as-is. A wrapper is renderable only if what it carries
    has a role, or is a shape the adapter handles specially.
    """
    if isinstance(message, dict):
        return True
    inner = message_dict(message)
    if inner is None:
        # Not dict-like at all — the adapter deep-copies it and passes it on.
        # Nothing here can say whether that works, so do not intervene.
        return True
    if "role" in inner:
        return True
    # Mirrors the adapter's own condition exactly, because anything looser
    # leaves a hole and anything tighter drops a message it could have handled.
    # Pipecat stores every thought as
    #     {"type": "thought", "text": ..., "signature": frame.signature}
    # and converts it only when *both* text and signature are truthy — so a
    # signature of None, or an empty thought, falls through to the raw dict and
    # takes the turn down.
    return (
        inner.get("type") == "thought"
        and bool(inner.get("text"))
        and bool(inner.get("signature"))
    )


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


def _last_clause_break(text: str, limit: int) -> int:
    """Index just past the last clause-ending punctuation within ``limit``.

    Returns 0 when there is none, letting the caller fall back to a word break.
    """
    best = 0
    for mark in (",", ";", ":", "—", "–"):
        pos = text.rfind(mark, 0, limit)
        if pos > best:
            best = pos + 1
    return best


def _split_for_tts(text: str, max_len: int, first_max_len: int | None = None) -> list[str]:
    """Split text into chunks no longer than max_len, preferring sentence then word
    boundaries. Keeps each chunk well under Kokoro's phoneme cap.

    ``first_max_len`` caps only the opening chunk. Synthesis latency scales with
    chunk length, so a short first chunk gets audio playing sooner while later
    chunks stay long enough to keep prosody natural. Playback of one chunk far
    outlasts synthesis of the next, so the shorter opener adds no gap.
    """
    text = text.strip()
    if not text:
        return []

    head_len = first_max_len or max_len
    if len(text) <= head_len:
        return [text]

    # First split on sentence boundaries, then pack greedily up to the cap that
    # applies to the chunk being built — the opener may have a tighter one.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    cur = ""

    def cap() -> int:
        """Length budget for the chunk currently being packed."""
        return head_len if not chunks else max_len

    for s in sentences:
        while len(s) > cap():
            # A single over-long sentence. Flush whatever is already packed
            # first, or these pieces would jump ahead of it and reorder the
            # text. Break at a clause boundary if there is one — a cut
            # mid-clause is audible, a cut after a comma is not — and fall back
            # to the last space before the cap.
            if cur:
                chunks.append(cur)
                cur = ""
            limit = cap()
            cut = _last_clause_break(s, limit)
            if cut <= 0:
                cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= cap():
            cur = f"{cur} {s}"
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]
