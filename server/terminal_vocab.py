"""Speech-recognition vocabulary for the terminal domain.

Two halves of the same problem. ``STT_KEYTERMS`` biases the recogniser toward
words it has no other reason to prefer — "claude" is a proper noun that loses to
the far commoner "cloud" every time. ``normalise_transcript`` repairs what the
recogniser still gets wrong.

The repair is deliberately context-gated. Replacing every "cloud" with "claude"
would be worse than the bug: this cockpit talks about cloud infrastructure too,
and "deploy to the cloud" must survive untouched.
"""

import re

# Terms fed to the recogniser as hints. Proper nouns and unix tools that sound
# like commoner English words, so they lose without a nudge.
STT_KEYTERMS = [
    "claude",
    "codex",
    "tmux",
    "fish",
    "git",
    "grep",
    "xargs",
    "jq",
    "sed",
    "awk",
    "stdout",
    "stderr",
    "repo",
    "cockpit",
    "mongo",
]

# Verbs and phrases that mean "the terminal is involved". A misheard "cloud"
# next to one of these is almost certainly "claude".
_TERMINAL_CONTEXT = (
    r"launch|start|run|open|opening|spawn|fire up|boot|"
    r"ask|tell|answer|reply|prompt|"
    r"terminal|shell|tmux|pane|window|session|"
    r"quit|exit|kill|stop|restart|"
    r"output|screen|watch|monitor"
)

# What the recogniser produces instead of "claude". Longest alternatives first
# so "cloud's" is not consumed as a bare "cloud", and the possessive/plural tail
# is captured separately so "clouds output" becomes "Claude's output".
_MISHEARD = r"(?:cloud|clod|claud|clawed)('s|s)?"

# How far apart the context word and the mishearing may sit. Generous enough for
# "cloud is asking something in the terminal", tight enough that two unrelated
# sentences in one utterance do not bleed into each other.
_GAP = r"[\w\s,'-]{0,40}?"

# "launch cloud" — context word before the mishearing.
_BEFORE = re.compile(
    rf"\b({_TERMINAL_CONTEXT})\b({_GAP})\b{_MISHEARD}\b",
    re.I,
)

# "cloud is asking" — context word after it.
_AFTER = re.compile(
    rf"\b{_MISHEARD}\b({_GAP})\b({_TERMINAL_CONTEXT})\b",
    re.I,
)

# Phrases where "cloud" is genuinely cloud infrastructure. Checked first, and a
# hit disables repair for the whole utterance — a false positive here puts a
# wrong word in the user's mouth, which is worse than leaving "cloud" alone.
_GENUINE_CLOUD = re.compile(
    r"\bcloud\s+(provider|infra|infrastructure|storage|native|formation|front|"
    r"watch|run|sql|build|function|compute|region|cost|bill|account|vendor)\b"
    r"|\b(aws|gcp|azure|atlas|google|amazon|multi|hybrid|private|public)[\s-]+cloud\b"
    r"|\bcloud\s+(9|nine)\b",
    re.I,
)


def _claude(heard: str, tail: str | None) -> str:
    """Build the replacement, keeping the casing and any possessive tail.

    "clouds output" is the possessive said aloud, so it becomes "Claude's
    output" rather than a bare "Claude output".
    """
    word = "claude"
    if heard.isupper():
        word = word.upper()
    elif heard[:1].isupper():
        word = word.capitalize()
    else:
        word = word.capitalize()  # a proper noun, whatever the recogniser said
    return word + ("'s" if tail else "")


def normalise_transcript(text: str) -> tuple[str, bool]:
    """Repair "cloud" -> "claude" when the context is clearly the terminal.

    Returns (text, changed) so callers can log the repair. Leaves the text
    alone when the utterance looks like it is really about cloud hosting.
    """
    if not text or not re.search(_MISHEARD, text, re.I):
        return text, False
    if _GENUINE_CLOUD.search(text):
        return text, False

    changed = False

    def repair_before(match: re.Match) -> str:
        nonlocal changed
        changed = True
        context, gap, tail = match.group(1), match.group(2), match.group(3)
        heard = match.group(0)[len(context) + len(gap):]
        return f"{context}{gap}{_claude(heard, tail)}"

    def repair_after(match: re.Match) -> str:
        nonlocal changed
        changed = True
        tail, gap, context = match.group(1), match.group(2), match.group(3)
        heard = match.group(0)[: len(match.group(0)) - len(gap) - len(context)]
        return f"{_claude(heard, tail)}{gap}{context}"

    result = _BEFORE.sub(repair_before, text)
    result = _AFTER.sub(repair_after, result)
    return result, changed
