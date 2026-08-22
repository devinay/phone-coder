"""Screen text utilities shared by the router, the monitor and the ring buffer.

The one idea here: a full-screen program is never byte-identical between two
frames. Claude Code redraws a spinner and a live elapsed/token counter, so
comparing screens as exact strings reports "still changing" forever — which is
why waiting for a TUI to settle used to burn the whole timeout.

``normalise`` masks the parts that tick on their own, so "only the counter
moved" compares equal while a genuine change still shows up. The same function
gives the ring buffer its de-duplication for free.
"""

import re

# Box-drawing characters, stripped so text inside a bordered dialog reads as
# text. Without this a prompt inside Claude Code's box never matches.
_BORDERS = str.maketrans("", "", "│╭╰╮╯─┃┏┗┓┛━┌┐└┘├┤┬┴┼║╔╚╗╝═▌▐")

# Braille spinners (Claude Code), ASCII spinners, and the sparkle/asterisk
# variants, at the start of a line.
_SPINNER_CHARS = r"[⠁-⣿✳✶✻✽✢·∗\*\|/\\\-—]"

# A status line that changes on its own: spinner, elapsed seconds, token
# counts, "esc to interrupt". Replaced wholesale rather than field by field,
# because the wording varies between versions.
_STATUS_LINE = re.compile(
    rf"^\s*{_SPINNER_CHARS}?\s*"
    r"(?:\w+(?:ing|ed)\b|thinking|working|crunching|pondering|baking|"
    r"esc to interrupt|press esc)"
    r".*?(?:\(|\b)(?:\d+[smh]\b|\d+\s*(?:tokens?|k\s*tokens?)).*$",
    re.I | re.M,
)

# A bare elapsed/token fragment anywhere on a line, e.g. "(12s · ↑ 1.4k tokens)".
_COUNTER = re.compile(
    r"\(\s*\d+(?:\.\d+)?\s*[smh]?\b[^)]*?(?:tokens?|↑|↓|·)[^)]*\)"
    r"|\b\d+(?:\.\d+)?k?\s+tokens?\b"
    r"|\b\d+[smh]\s+(?:elapsed|remaining)\b",
    re.I,
)

# A lone spinner frame on an otherwise empty line.
_LONE_SPINNER = re.compile(rf"^\s*{_SPINNER_CHARS}\s*$", re.M)

# Progress bars and percentage readouts.
_PROGRESS = re.compile(r"[█▉▊▋▌▍▎▏░▒▓]{2,}|\b\d{1,3}\s?%")

_MASK = "<STATUS>"


def strip_borders(text: str) -> str:
    """Remove box-drawing characters so bordered text reads as plain text."""
    return text.translate(_BORDERS)


def normalise(text: str) -> str:
    """Collapse the self-animating parts of a screen to a stable form.

    Used for "has anything really changed?" comparisons and for ring-buffer
    de-duplication. Not for display — callers show the raw screen.
    """
    if not text:
        return ""
    text = _STATUS_LINE.sub(_MASK, text)
    text = _COUNTER.sub(_MASK, text)
    text = _LONE_SPINNER.sub(_MASK, text)
    text = _PROGRESS.sub(_MASK, text)
    # Trailing whitespace shifts as a TUI repaints; it is never meaningful.
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip()


def same_screen(a: str, b: str) -> bool:
    """Whether two screens differ only in their self-animating parts."""
    return normalise(a) == normalise(b)
