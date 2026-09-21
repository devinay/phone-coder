"""Terminal history: a rendered record of everything the pane has shown.

``tmux capture-pane`` can only ever return the *current* screen, and a
full-screen program such as Claude Code keeps no scrollback at all, so the
visible screen was the only thing an agent could ever read. Asking for more
lines returned the same screen.

This module taps the pane's byte stream with ``tmux pipe-pane`` and replays it
through ``pyte``, a headless terminal emulator, so we hold the same rendered
text a human sees — including everything that has scrolled away or been painted
over. Two tiers, because the two kinds of output have genuinely different
shapes:

- **Line output** (fish, git, builds): scrolls and never repaints, so a ring of
  lines is the natural record.
- **Screen output** (Claude Code, vim): repaints in place, so the record is a
  ring of rendered snapshots, appended only when the screen actually changed.

Both live in memory: nothing to clean up if the process dies, and the history
disappears with the session by construction. The on-disk spool exists only
because ``pipe-pane`` needs somewhere to write.
"""

import asyncio
import os
import re
from collections import deque

import pyte
from loguru import logger

from terminal_screen import normalise

# pyte does not implement the alternate screen buffer, so we detect the switch
# ourselves and swap between two Screen instances. Without this, a TUI's last
# frame stays on the primary screen forever after the program exits.
_ENTER_ALT = re.compile(r"\x1b\[\?(?:1049|1047|47)h")
_LEAVE_ALT = re.compile(r"\x1b\[\?(?:1049|1047|47)l")

DEFAULT_COLS = 200
DEFAULT_ROWS = 50


class TerminalHistory:
    """Replays a pane's byte stream into rendered, bounded history."""

    def __init__(
        self,
        spool_path: str,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        max_lines: int = 1000,
        max_snapshots: int = 40,
    ):
        self._spool_path = spool_path
        self._cols, self._rows = cols, rows

        # Primary screen: the shell. HistoryScreen keeps the lines that scroll
        # off the top, bounded to max_lines — the ring the shell tier needs,
        # maintained by pyte rather than by hand.
        self._primary = pyte.HistoryScreen(cols, rows, history=max_lines, ratio=1.0)
        self._primary_stream = pyte.Stream(self._primary)
        # Alternate screen: whatever TUI took over. No history — a TUI repaints
        # in place, so scrollback is meaningless and snapshots are the record.
        self._alt = pyte.Screen(cols, rows)
        self._alt_stream = pyte.Stream(self._alt)
        self._in_alt = False

        self._max_lines = max_lines
        # Tier 2: rendered TUI screens, deduplicated.
        self._snapshots: deque[str] = deque(maxlen=max_snapshots)
        self._last_snapshot_norm = ""
        # Where the reader has consumed to, and where each caller last looked.
        self._offset = 0
        self._marks: dict[str, str] = {}
        self._reader: asyncio.Task | None = None
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._reader = asyncio.ensure_future(self._read_loop())
        logger.info(
            f"[HISTORY] started spool={self._spool_path} "
            f"grid={self._cols}x{self._rows} lines={self._max_lines} "
            f"snapshots={self._snapshots.maxlen}"
        )

    def stop_nowait(self) -> None:
        """Stop reading and remove the spool, without awaiting the reader.

        For the case where the pane being tapped has already gone: there is
        nothing left to read, so waiting for the reader to wind down buys
        nothing, and the caller (session reset) is synchronous. The reader is
        cancelled and left to be collected.
        """
        self._started = False
        if self._reader:
            self._reader.cancel()
            self._reader = None
        self._remove_spool()

    async def stop(self) -> None:
        """Stop reading and remove the spool.

        The history itself is in memory and goes with the object; only the
        spool file needs deleting, and it must not outlive the session.
        """
        self._started = False
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        self._remove_spool()

    def _remove_spool(self) -> None:
        try:
            os.unlink(self._spool_path)
            logger.info(f"[HISTORY] removed spool {self._spool_path}")
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"[HISTORY] could not remove spool {self._spool_path}: {e}")

    # ── ingest ───────────────────────────────────────────────────────────────

    async def _read_loop(self, poll_secs: float = 0.4) -> None:
        """Tail the spool and feed new bytes through the emulator."""
        while True:
            try:
                await asyncio.sleep(poll_secs)
                self.pump()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a broken tap must not kill the session
                logger.error(f"[HISTORY] read loop error: {e}")

    def pump(self) -> None:
        """Consume whatever the spool has gained since the last call."""
        try:
            size = os.path.getsize(self._spool_path)
        except OSError:
            return
        if size < self._offset:
            # Spool was truncated or replaced; start over rather than read garbage.
            logger.warning("[HISTORY] spool shrank; resetting offset")
            self._offset = 0
        if size == self._offset:
            return
        try:
            with open(self._spool_path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError as e:
            logger.warning(f"[HISTORY] spool read failed: {e}")
            return
        self._offset += len(chunk)
        self.feed(chunk.decode("utf-8", "replace"))

    def feed(self, text: str) -> None:
        """Route a chunk through the right screen, splitting on alt-screen switches.

        A single chunk can contain an alt-screen transition, so it is split at
        each switch and the pieces are fed to whichever screen was active.
        """
        for piece, entering in _split_on_alt_switch(text):
            if piece:
                if self._in_alt:
                    self._alt_stream.feed(piece)
                    self._capture_snapshot()
                else:
                    self._primary_stream.feed(piece)
            if entering is not None:
                self._switch_screen(entering)

    def _switch_screen(self, entering: bool) -> None:
        if entering == self._in_alt:
            return
        self._in_alt = entering
        if entering:
            # A fresh alternate screen starts blank, as a real terminal does.
            self._alt.reset()
            self._last_snapshot_norm = ""
            logger.debug("[HISTORY] entered alternate screen")
        else:
            # Snapshot the final TUI frame before letting go of it, otherwise
            # the last thing Claude Code said is lost on exit.
            self._capture_snapshot(force=True)
            logger.debug("[HISTORY] left alternate screen")

    def _shell_lines(self) -> list[str]:
        """Scrolled-off lines plus the visible screen, as plain text.

        pyte holds a grid, not a stream of lines, so history rows are rendered
        cell by cell. Blank lines are dropped: a terminal grid is mostly
        padding, and the padding is not history.
        """
        lines = []
        for row in self._primary.history.top:
            text = "".join(row[x].data for x in sorted(row)).rstrip()
            if text:
                lines.append(text)
        for line in self._primary.display:
            if line.rstrip():
                lines.append(line.rstrip())
        return lines

    def _capture_snapshot(self, force: bool = False) -> None:
        """Record the alternate screen, but only when it really changed."""
        rendered = "\n".join(line.rstrip() for line in self._alt.display).rstrip()
        if not rendered:
            return
        marker = normalise(rendered)
        if not force and marker == self._last_snapshot_norm:
            return  # only the spinner or a counter moved
        self._last_snapshot_norm = marker
        self._snapshots.append(rendered)

    # ── read ─────────────────────────────────────────────────────────────────

    @property
    def in_alternate_screen(self) -> bool:
        return self._in_alt

    def tail(self, lines: int = 200) -> str:
        """The most recent history, rendered, whichever tier is active."""
        self.pump()
        if self._in_alt and self._snapshots:
            return self._snapshots[-1]
        return "\n".join(self._shell_lines()[-lines:])

    def full(self, lines: int | None = None) -> str:
        """As much history as is held, shell lines and TUI screens combined."""
        self.pump()
        lines = lines or self._max_lines
        parts = []
        shell = self._shell_lines()
        if shell:
            parts.append("\n".join(shell[-lines:]))
        if self._snapshots:
            parts.append(
                f"[{len(self._snapshots)} full-screen frame(s) recorded; "
                "the most recent follows]"
            )
            parts.append(self._snapshots[-1])
        return "\n\n".join(parts) if parts else "(no terminal history yet)"

    def since_last_look(self, who: str = "default") -> tuple[str, bool]:
        """What changed since this caller last looked.

        Returns (text, changed). The point of the whole ring: "nothing has
        changed" is a cheap and truthful answer, and a real change costs a diff
        rather than a whole screen.
        """
        self.pump()
        current = self.tail()
        marker = normalise(current)
        previous = self._marks.get(who)
        self._marks[who] = marker
        if previous is None:
            return current, True
        if previous == marker:
            return "", False
        return _diff(previous, marker, current), True


def _split_on_alt_switch(text: str):
    """Yield (piece, entering) splitting text at alt-screen switches.

    ``entering`` is True for a switch into the alternate screen, False for a
    switch out, and None for the trailing piece with no switch after it.
    """
    pos = 0
    pattern = re.compile(
        f"({_ENTER_ALT.pattern})|({_LEAVE_ALT.pattern})"
    )
    for match in pattern.finditer(text):
        yield text[pos:match.start()], bool(match.group(1))
        pos = match.end()
    yield text[pos:], None


def _diff(previous_norm: str, current_norm: str, current_raw: str) -> str:
    """The lines present now that were not present at the last look."""
    before = set(previous_norm.splitlines())
    added = [ln for ln in current_norm.splitlines() if ln and ln not in before]
    if not added:
        # Content moved rather than arrived (a repaint); the screen is the
        # honest answer.
        return current_raw
    return "\n".join(added)
