import subprocess
import os
import re
import difflib
import asyncio
import time
from loguru import logger


class AgentRouter:
    SESSION      = "cockpit"
    DEFAULT_PANE = "shell"

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
        return self.capture_output()

    async def send_input(self, text: str):
        """Send raw text to whatever is currently running in the shell pane."""
        target = self._target()
        self._run_tmux("send-keys", "-t", target, "-l", text)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")
        await asyncio.sleep(3)
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
            return (
                "[NOTE: a full-screen program is running, which keeps no scrollback. "
                "This is the visible screen only. To read more of its output, ask the "
                "program itself for history, or re-run the command with output piped "
                "to a file and read the file.]\n" + output
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
            if current != previous:
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

        started = time.monotonic()
        previous = self.capture_output()
        unchanged_since = time.monotonic()
        if matcher and matcher.search(previous):
            return previous, f"matched {pattern!r}"
        while True:
            await asyncio.sleep(poll_secs)
            current = self.capture_output()
            now = time.monotonic()
            if matcher and matcher.search(current):
                return current, f"matched {pattern!r}"
            if current != previous:
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
