import asyncio
import difflib
import os
import re
import subprocess
import time

from loguru import logger

from terminal_history import TerminalHistory
from terminal_monitor import find_prompt
from terminal_screen import same_screen, strip_borders
from terminal_state import ForegroundStack, is_interactive
from terminal_tasks import WatchRegistry


class AgentRouter:
    SESSION      = "cockpit"
    DEFAULT_PANE = "shell"

    def __init__(self):
        self.foreground = ForegroundStack()
        self.watches = WatchRegistry()
        self.history: TerminalHistory | None = None
        self._spool_path = f"/tmp/cockpit-pane-{os.getpid()}.spool"
        self._last_user_speech = time.monotonic()

    # ── conversation timing ───────────────────────────────────────────────────

    def note_user_spoke(self) -> None:
        """Record that the user just said something.

        The monitor uses this to stay out of the way: while the user is talking,
        the next turn boundary will service the terminal anyway, so interrupting
        is both unnecessary and rude.
        """
        self._last_user_speech = time.monotonic()

    def seconds_since_user_spoke(self) -> float:
        return time.monotonic() - self._last_user_speech

    def _target(self) -> str:
        return f"{self.SESSION}:{self.DEFAULT_PANE}"

    def ensure_session(self):
        """Create the tmux session with a fish shell. Called on client connect."""
        result = subprocess.run(["tmux", "has-session", "-t", self.SESSION], capture_output=True)
        if result.returncode != 0:
            logger.info(f"Creating tmux session: {self.SESSION}")
            subprocess.run([
                "tmux", "new-session", "-d",
                "-s", self.SESSION,
                "-n", self.DEFAULT_PANE,
                "fish",
            ])
        else:
            logger.info(f"Tmux session {self.SESSION} already exists")
        subprocess.run(["tmux", "set-option", "-t", self.SESSION, "allow-rename", "off"])
        subprocess.run(["tmux", "set-option", "-t", self.SESSION, "mouse", "on"])

    def start_history(self):
        """Tap the pane so its output is recorded even when it repaints.

        capture-pane can only return the current screen, and a full-screen
        program keeps no scrollback, so without this tap everything Claude Code
        prints is unreadable the moment it scrolls. pipe-pane is passive and does
        not disturb the ttyd attachment the user is watching.
        """
        if self.history is not None:
            return
        try:
            open(self._spool_path, "w").close()
            self._run_tmux(
                "pipe-pane", "-o", "-t", self._target(), f"cat >> {self._spool_path}"
            )
            self.history = TerminalHistory(self._spool_path)
            self.history.start()
        except Exception as e:
            # History is an enhancement; the terminal must still work without it.
            logger.error(f"[HISTORY] could not start pane tap: {e}")
            self.history = None

    async def stop_history(self):
        if self.history is None:
            return
        self._run_tmux("pipe-pane", "-t", self._target())  # detach the tap
        await self.history.stop()
        self.history = None

    def reset_session(self):
        """Kill the tmux session and start a fresh one."""
        logger.info(f"Resetting tmux session: {self.SESSION}")
        subprocess.run(["tmux", "kill-session", "-t", self.SESSION], capture_output=True)
        self.ensure_session()

    def _run_tmux(self, *args):
        cmd = ["tmux"] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Tmux command failed: {' '.join(cmd)} — {result.stderr}")
        return result.stdout.strip()

    # ── Directory search ──────────────────────────────────────────────────────

    def find_best_directory(self, path_or_name: str, base_dir: str = None):
        """Find a directory by name up to 3 levels deep, with fuzzy matching."""
        full_path = os.path.abspath(os.path.expanduser(path_or_name))
        if os.path.isdir(full_path):
            return full_path, True

        if not base_dir:
            base_dir = os.path.abspath(os.path.expanduser("~"))

        matches = []
        all_dirs = {}
        target_name = os.path.basename(path_or_name).lower()
        exclude_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__", "Library"}

        for root, dirs, _ in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            depth = root[len(base_dir):].count(os.sep)
            if depth >= 3:
                dirs[:] = []
                continue
            for d in dirs:
                d_lower = d.lower()
                d_path = os.path.join(root, d)
                all_dirs.setdefault(d_lower, []).append(d_path)
                if d_lower == target_name:
                    matches.append(d_path)

        if len(matches) == 1:
            return matches[0], False
        elif len(matches) > 1:
            return matches, False

        similar = difflib.get_close_matches(target_name, all_dirs.keys(), n=3, cutoff=0.6)
        if similar:
            return [p for name in similar for p in all_dirs[name]], False
        return None, False

    # ── Terminal commands ─────────────────────────────────────────────────────

    async def run_command(self, command: str, directory_path: str = "", wait_secs: int = 2):
        """cd to directory_path and run command in the shell pane."""
        if directory_path:
            full_path = os.path.abspath(os.path.expanduser(directory_path))
            full_cmd = f"cd '{full_path}' && {command}" if os.path.isdir(full_path) else command
        else:
            full_cmd = command

        target = self._target()
        self._run_tmux("send-keys", "-t", target, "")
        self._run_tmux("send-keys", "-t", target, "C-u")
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "-l", full_cmd)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")

        await asyncio.sleep(wait_secs)
        # Record interactive launches so every agent knows what is running,
        # rather than having to remember it from a tool result that gets
        # stripped at the next agent handoff.
        if is_interactive(command):
            self.foreground.push(
                command,
                cwd=directory_path or self.current_directory(),
                full_screen=self.on_alternate_screen(),
            )
        return self.capture_output()

    async def send_input(self, text: str):
        """Send raw text plus Enter to whatever is running in the shell pane."""
        target = self._target()
        self._run_tmux("send-keys", "-t", target, "-l", text)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")
        await asyncio.sleep(3)
        return self.capture_output()

    async def send_key(self, *keys: str, settle: float = 1.0):
        """Send bare keypresses, with no trailing Enter.

        Interactive programs are driven by keys, not lines. Claude Code's
        permission dialog is a numbered selector: it wants ``1``, or ``Enter``
        to take the highlighted option, or ``Up``/``Down`` to move. Sending the
        word "yes" through send_input types three letters into a widget that
        does not read letters, which is why answering used to do nothing.

        Key names are tmux's own: ``Enter``, ``Escape``, ``Up``, ``Down``,
        ``Tab``, ``C-c``, or a literal character such as ``1``.
        """
        target = self._target()
        for key in keys:
            self._run_tmux("send-keys", "-t", target, key)
            await asyncio.sleep(0.1)
        await asyncio.sleep(settle)
        return self.capture_output()

    def _strip_ansi(self, text: str) -> str:
        return re.sub(r'\x1b\[[0-9;]*[mKHJA-Z]|\x1b[()][AB012]', '', text)

    def _session_running(self) -> bool:
        result = subprocess.run(["tmux", "has-session", "-t", self.SESSION], capture_output=True)
        return result.returncode == 0

    def _pane_var(self, name: str) -> str:
        """Read a tmux format variable for the shell pane, e.g. alternate_on."""
        try:
            return self._run_tmux(
                "display-message", "-p", "-t", self._target(), f"#{{{name}}}"
            ).strip()
        except Exception:
            return ""

    def on_alternate_screen(self) -> bool:
        """Whether a full-screen TUI (Claude Code, vim, less) owns the pane.

        Alternate-screen programs keep no tmux scrollback, so only the visible
        screen can be captured while one is running.
        """
        return self._pane_var("alternate_on") == "1"

    def pane_command(self) -> str:
        """The foreground process tmux reports for the pane."""
        return self._pane_var("pane_current_command")

    def current_directory(self) -> str:
        """The pane's working directory, from tmux rather than the prompt.

        Fish abbreviates paths in its prompt, so the prompt is not a reliable
        source for a full path.
        """
        return self._pane_var("pane_current_path")

    def terminal_status(self) -> str:
        """One line describing what is running, for injection into every turn.

        Reconciles the stack against tmux first, so a program the user quit by
        hand is not still reported as running.
        """
        if not self._session_running():
            return "[TERMINAL] no session running"
        exited = self.foreground.reconcile(self.pane_command(), self.on_alternate_screen())
        if exited:
            logger.info(f"[FOREGROUND] exited: {[p.name for p in exited]}")
        return self.foreground.describe(self.current_directory())

    def terminal_context_block(self) -> str:
        """Everything the model needs to service the terminal this turn.

        Three parts: what is running, what standing instructions are in force,
        and whether something is waiting right now. The last one is what makes
        turn-boundary servicing work — the model is told a decision is pending
        before it composes its reply, so it can act and answer in one turn
        instead of needing to be woken separately.
        """
        if not self._session_running():
            return "[TERMINAL] no session running"

        parts = [self.terminal_status()]
        self.watches.prune()
        watching = self.watches.describe()
        if watching:
            parts.append(watching)
            # Only look for a pending question when something is actually being
            # watched; otherwise this is a capture-pane call on every turn for
            # nothing.
            screen = self.capture_output()
            prompt = find_prompt(screen, self.on_alternate_screen())
            if prompt:
                parts.append(
                    "[WAITING] The terminal is asking something right now. Apply the "
                    "watch instruction above, act on it with send_keys, and say what "
                    "you did. What is on screen:\n" + prompt
                )
        return "\n".join(parts)

    def capture_output(self, lines: int = None):
        """Capture terminal output from the shell pane.

        Without lines: the current visible screen.
        With lines: also pulls that many lines of scrollback above the visible
        area. Scrollback does not exist while a full-screen TUI is running, so
        that case says so rather than silently returning one screen.
        """
        if not self._session_running():
            return "Error: Terminal session not running."
        alt = self.on_alternate_screen()
        args = ["capture-pane", "-p", "-t", self._target()]
        if lines and not alt:
            args += ["-S", f"-{lines}"]
        output = self._strip_ansi(self._run_tmux(*args))
        if lines and alt:
            # tmux has no scrollback to give here, but the pipe-pane tap has been
            # recording the rendered screens all along, so the history is real.
            if self.history is not None:
                recorded = self.history.full(lines)
                return (
                    "[NOTE: a full-screen program is running, so this history comes from "
                    "the recorded pane stream rather than tmux scrollback.]\n" + recorded
                )
            return (
                "[NOTE: a full-screen program is running, which keeps no scrollback, and "
                "pane recording is unavailable. This is the visible screen only.]\n"
                + output
            )
        return output

    async def wait_for_idle(
        self,
        idle_secs: float = 2.0,
        timeout: float = 120.0,
        poll_secs: float = 0.5,
    ) -> tuple[str, str]:
        """Poll until the screen stops changing, or timeout.

        Returns (final output, reason). Commands routinely outlive the fixed
        sleep in run_command, so this is how to read a *finished* result rather
        than a half-rendered one.
        """
        if not self._session_running():
            return "Error: Terminal session not running.", "no_session"
        started = time.monotonic()
        previous = self.capture_output()
        unchanged_since = time.monotonic()
        while True:
            await asyncio.sleep(poll_secs)
            current = self.capture_output()
            now = time.monotonic()
            # Compared through normalise() so a ticking spinner or token
            # counter does not read as activity. Comparing raw text here meant
            # a TUI never settled and this always ran to timeout.
            if not same_screen(current, previous):
                previous = current
                unchanged_since = now
            elif now - unchanged_since >= idle_secs:
                return current, f"idle for {idle_secs}s"
            if now - started >= timeout:
                return current, f"timeout after {timeout}s (still changing)"

    async def watch(
        self,
        pattern: str = "",
        idle_secs: float = 2.0,
        timeout: float = 300.0,
        poll_secs: float = 0.5,
    ) -> tuple[str, str]:
        """Watch the pane until a pattern appears, output settles, or timeout.

        With a pattern this is the "tell me when X happens" primitive; without
        one it behaves like wait_for_idle with a longer default timeout.
        """
        if not self._session_running():
            return "Error: Terminal session not running.", "no_session"
        try:
            matcher = re.compile(pattern, re.I) if pattern else None
        except re.error as e:
            return f"Error: invalid pattern ({e}).", "bad_pattern"

        def matched(screen: str) -> bool:
            """Match against the screen and its border-stripped form.

            Text inside a TUI dialog is fenced by box-drawing characters, so a
            pattern that spans a border ("proceed? │") only matches once those
            are gone.
            """
            return bool(
                matcher and (matcher.search(screen) or matcher.search(strip_borders(screen)))
            )

        started = time.monotonic()
        previous = self.capture_output()
        unchanged_since = time.monotonic()
        if matched(previous):
            return previous, f"matched {pattern!r}"
        while True:
            await asyncio.sleep(poll_secs)
            current = self.capture_output()
            now = time.monotonic()
            if matched(current):
                return current, f"matched {pattern!r}"
            if not same_screen(current, previous):
                previous = current
                unchanged_since = now
            elif not matcher and now - unchanged_since >= idle_secs:
                return current, f"idle for {idle_secs}s"
            if now - started >= timeout:
                reason = f"timeout after {timeout}s"
                return current, reason + (f" without matching {pattern!r}" if matcher else "")

    def cleanup(self):
        logger.info(f"Killing tmux session: {self.SESSION}")
        subprocess.run(["tmux", "kill-session", "-t", self.SESSION], capture_output=True)
