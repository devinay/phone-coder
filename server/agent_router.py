import asyncio
import difflib
import os
import re
import subprocess
import time

from loguru import logger

from terminal_history import TerminalHistory
from terminal_monitor import find_prompt, how_to_answer
from terminal_screen import same_screen, strip_borders
from terminal_state import PS_FORMAT, Observation, current_shell, pane_owner, parse_ps
from terminal_tasks import WatchRegistry


class AgentRouter:
    SESSION      = "cockpit"
    DEFAULT_PANE = "shell"
    # Where a fresh session starts. The repo root rather than server/, because
    # that is where git, the tests and Claude Code are actually run from. Fixed
    # rather than inherited from the server process, so the directory the agent
    # is told about at startup does not depend on where the server was launched.
    START_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def __init__(self):
        self.watches = WatchRegistry()
        # Set by create_shell_tools, so teardown can stop the background loops.
        self.monitor = None
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

    def _target(self, target: str = "") -> str:
        """Resolve a pane target, defaulting to the cockpit's shell pane.

        Every pane-touching call takes an optional target so a second thing can
        be watched — a build in one pane while Claude Code runs in another —
        without any of them needing to know the default's name.
        """
        return target or f"{self.SESSION}:{self.DEFAULT_PANE}"

    def default_target(self) -> str:
        return self._target()

    def resolve_target(self, target: str = "") -> str:
        """The full tmux target a caller's pane name refers to."""
        return self._target(target)

    def pane_exists(self, target: str = "") -> bool:
        """Whether a pane target actually resolves to a live pane.

        Needed because a target is just a string: the model can pass a pane name
        it invented, and every later call then fails against a pane that was
        never there. Asking tmux is the only way to know.
        """
        resolved = self._target(target)
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", resolved, "#{pane_id}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def list_panes(self) -> list[dict[str, str]]:
        """Every pane in the cockpit session, so a watch can name one.

        Returns target, the command tmux believes is running, and the pane's
        working directory — enough for the model to say which is the build.
        """
        raw = self._run_tmux(
            "list-panes", "-s", "-t", self.SESSION, "-F",
            "#{session_name}:#{window_name}.#{pane_index}\t#{pane_current_command}"
            "\t#{pane_current_path}",
        )
        panes = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                panes.append({"target": parts[0], "command": parts[1], "cwd": parts[2]})
        return panes

    def ensure_session(self, reset: bool = False) -> list[str]:
        """Create the tmux session with a fish shell. Called on client connect.

        With ``reset``, any surviving session is killed first. That is what makes
        "an empty terminal is attached" true at startup rather than merely usual:
        a clean disconnect already tears the session down, but a crash or a
        Ctrl-C leaves it standing with whatever was running still in it, and the
        next boot would otherwise inherit it and describe it as fresh.

        Returns what was killed, so the loss is reported rather than silent.
        """
        killed: list[str] = []
        alive = (
            subprocess.run(
                ["tmux", "has-session", "-t", self.SESSION], capture_output=True
            ).returncode
            == 0
        )
        if alive and reset:
            # Read what is going before killing it; afterwards there is nothing
            # left to ask, and "we killed something" is worth more than a count.
            killed = sorted(
                {
                    p["command"]
                    for p in self.list_panes()
                    if p["command"] and p["command"] not in ("fish", "bash", "zsh", "sh")
                }
            )
            logger.info(
                f"[SESSION] resetting {self.SESSION}; killing "
                f"{', '.join(killed) if killed else 'an idle shell'}"
            )
            subprocess.run(["tmux", "kill-session", "-t", self.SESSION], capture_output=True)
            alive = False

        if not alive:
            logger.info(f"Creating tmux session: {self.SESSION} in {self.START_DIR}")
            subprocess.run([
                "tmux", "new-session", "-d",
                "-s", self.SESSION,
                "-n", self.DEFAULT_PANE,
                "-c", self.START_DIR,
                "fish",
            ])
            # A new session has a new pane, so any tap on the old one is pointing
            # at a pane that no longer exists. Dropping the handle lets
            # start_history reattach instead of returning early and leaving
            # history quietly dead until a full server restart.
            self._invalidate_history()
        else:
            logger.info(f"Tmux session {self.SESSION} already exists")
        subprocess.run(["tmux", "set-option", "-t", self.SESSION, "allow-rename", "off"])
        subprocess.run(["tmux", "set-option", "-t", self.SESSION, "mouse", "on"])
        return killed

    def _invalidate_history(self) -> None:
        """Forget a tap whose pane has gone, without waiting on the reader.

        Deliberately not ``stop_history``: that detaches the tap from a pane that
        no longer exists and awaits a reader that is already at EOF. Here the
        handle is simply dropped so the next ``start_history`` builds a new one.
        """
        if self.history is None:
            return
        logger.info("[HISTORY] pane replaced; dropping the old tap")
        try:
            self.history.stop_nowait()
        except Exception as e:
            logger.warning(f"[HISTORY] could not close the old tap cleanly: {e}")
        self.history = None

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

    def reset_session(self) -> list[str]:
        """Kill the tmux session and start a fresh one.

        Returns what was running when it went. Reattaches the pane tap, without
        which history stayed pointed at the dead pane and quietly recorded
        nothing until the whole server was restarted.
        """
        logger.info(f"Resetting tmux session: {self.SESSION}")
        killed = self.ensure_session(reset=True)
        self.start_history()
        return killed

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

    async def run_command(
        self, command: str, directory_path: str = "", wait_secs: int = 2, target: str = ""
    ):
        """cd to directory_path and run command in the shell pane."""
        if directory_path:
            full_path = os.path.abspath(os.path.expanduser(directory_path))
            full_cmd = f"cd '{full_path}' && {command}" if os.path.isdir(full_path) else command
        else:
            full_cmd = command

        target = self._target(target)
        self._run_tmux("send-keys", "-t", target, "")
        self._run_tmux("send-keys", "-t", target, "C-u")
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "-l", full_cmd)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")

        await asyncio.sleep(wait_secs)
        # Nothing to record: what is running is read from the pane's tty on
        # every turn, so a launch needs no bookkeeping and an interactive
        # program the user started by hand is seen just the same.
        return self.capture_output(target=target)

    async def send_input(self, text: str, target: str = ""):
        """Send raw text plus Enter to whatever is running in the shell pane."""
        target = self._target(target)
        self._run_tmux("send-keys", "-t", target, "-l", text)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")
        await asyncio.sleep(3)
        return self.capture_output(target=target)

    async def send_key(self, *keys: str, settle: float = 1.0, target: str = ""):
        """Send bare keypresses, with no trailing Enter.

        Interactive programs are driven by keys, not lines. Claude Code's
        permission dialog is a numbered selector: it wants ``1``, or ``Enter``
        to take the highlighted option, or ``Up``/``Down`` to move. Sending the
        word "yes" through send_input types three letters into a widget that
        does not read letters, which is why answering used to do nothing.

        Key names are tmux's own: ``Enter``, ``Escape``, ``Up``, ``Down``,
        ``Tab``, ``C-c``, or a literal character such as ``1``.
        """
        target = self._target(target)
        for key in keys:
            self._run_tmux("send-keys", "-t", target, key)
            await asyncio.sleep(0.1)
        await asyncio.sleep(settle)
        return self.capture_output(target=target)

    def _strip_ansi(self, text: str) -> str:
        return re.sub(r'\x1b\[[0-9;]*[mKHJA-Z]|\x1b[()][AB012]', '', text)

    def _session_running(self) -> bool:
        result = subprocess.run(["tmux", "has-session", "-t", self.SESSION], capture_output=True)
        return result.returncode == 0

    def _pane_var(self, name: str, target: str = "") -> str:
        """Read a tmux format variable for the shell pane, e.g. alternate_on."""
        try:
            return self._run_tmux(
                "display-message", "-p", "-t", self._target(target), f"#{{{name}}}"
            ).strip()
        except Exception:
            return ""

    def on_alternate_screen(self, target: str = "") -> bool:
        """Whether a full-screen TUI (Claude Code, vim, less) owns the pane.

        Alternate-screen programs keep no tmux scrollback, so only the visible
        screen can be captured while one is running.
        """
        return self._pane_var("alternate_on", target) == "1"

    def pane_command(self, target: str = "") -> str:
        """The foreground process tmux reports for the pane."""
        return self._pane_var("pane_current_command", target)

    def current_directory(self, target: str = "") -> str:
        """The pane's working directory, from tmux rather than the prompt.

        Fish abbreviates paths in its prompt, so the prompt is not a reliable
        source for a full path.
        """
        return self._pane_var("pane_current_path", target)

    def pane_tty(self, target: str = "") -> str:
        """The tty device backing the pane, e.g. ``ttys004``.

        The scoping key for the process reading: every process in the pane is
        attached to this tty, however deeply nested, whereas parent-pid links
        break the moment the shell forks.
        """
        return self._pane_var("pane_tty", target).removeprefix("/dev/")

    def _read_processes(self, target: str = ""):
        """Every process on the pane's tty. The one seam the tests stub."""
        tty = self.pane_tty(target)
        if not tty:
            return []
        result = subprocess.run(
            ["ps", "-t", tty, "-o", PS_FORMAT], capture_output=True, text=True
        )
        if result.returncode != 0:
            # ps exits non-zero when the tty has gone away, which is normal
            # during teardown and not worth an error line.
            logger.debug(f"[PS] no processes for tty {tty}: {result.stderr.strip()}")
            return []
        return parse_ps(result.stdout)

    def observe(self, target: str = "") -> Observation:
        """Read what is running, all at one moment.

        Every field comes from this single call so the parts cannot disagree
        with each other. Callers that need both the process picture and the
        screen should take one Observation and pass it around rather than
        re-reading, which is how a stale pairing gets introduced.
        """
        if not self._session_running():
            return Observation(session_running=False)
        processes = self._read_processes(target)
        return Observation(
            shell=current_shell(processes),
            cwd=self.current_directory(target),
            owner=pane_owner(processes),
            busy_with=self.pane_command(target),
            full_screen=self.on_alternate_screen(target),
            processes=processes,
        )

    def terminal_status(self) -> str:
        """One line describing what is running, for injection into every turn."""
        return self.observe().describe()

    def terminal_context_block(self) -> str:
        """Everything the model needs to service the terminal this turn.

        Three parts: what is running, what standing instructions are in force,
        and whether something is waiting right now. The last one is what makes
        turn-boundary servicing work — the model is told a decision is pending
        before it composes its reply, so it can act and answer in one turn
        instead of needing to be woken separately.
        """
        seen = self.observe()
        if not seen.session_running:
            return "[TERMINAL] no session running"

        parts = [seen.describe()]
        self.watches.prune()
        watching = self.watches.describe()
        if watching:
            parts.append(watching)
            # Only look for a pending question on panes that are actually being
            # watched; otherwise this is a capture-pane call on every turn for
            # nothing. Every watched pane is checked, not just the default —
            # otherwise a question on the second pane is invisible at exactly
            # the moment the model is supposed to answer it.
            default = self.default_target()
            for target in self.watches.targets():
                # full_screen has to come from the same observation as the
                # screen, so the question and the program it belongs to are one
                # reading. The default pane already has one.
                pane = seen if self._target(target) == default else self.observe(target)
                screen = self.capture_output(target=target)
                prompt = find_prompt(screen, pane.full_screen)
                if not prompt:
                    continue
                where = "The terminal" if self._target(target) == default else f"Pane {target}"
                block = (
                    f"[WAITING] {where} is asking something right now. Apply the "
                    "watch instruction above, act on it with send_keys, and say what "
                    "you did."
                )
                # Some dialogs do not answer to the key you would expect. Saying
                # so here, next to the screen, is the only place it is certain
                # to be read at the moment the keys are chosen.
                keys = how_to_answer(screen)
                if keys:
                    block += "\n[HOW TO ANSWER IT] " + keys
                parts.append(block + "\nWhat is on screen:\n" + prompt)
        return "\n".join(parts)

    def capture_output(self, lines: int = None, target: str = ""):
        """Capture terminal output from the shell pane.

        Without lines: the current visible screen.
        With lines: also pulls that many lines of scrollback above the visible
        area. Scrollback does not exist while a full-screen TUI is running, so
        that case says so rather than silently returning one screen.
        """
        if not self._session_running():
            return "Error: Terminal session not running."
        alt = self.on_alternate_screen(target)
        args = ["capture-pane", "-p", "-t", self._target(target)]
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
