import subprocess
import os
import difflib
import asyncio
from loguru import logger


class AgentRouter:
    SESSION      = "cockpit"
    DEFAULT_PANE = "shell"

    def _target(self, pane_name: str = None) -> str:
        return f"{self.SESSION}:{pane_name or self.DEFAULT_PANE}"

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

    def open_pane(self, name: str) -> str:
        """Create a new tmux window named `name`. Return its target string."""
        target = self._target(name)
        result = subprocess.run(["tmux", "has-session", "-t", target], capture_output=True)
        if result.returncode != 0:
            subprocess.run([
                "tmux", "new-window",
                "-t", self.SESSION,
                "-n", name,
                "fish",
            ])
            logger.info(f"Created tmux window: {name}")
        else:
            logger.info(f"Tmux window {name} already exists")
        return target

    def list_panes(self) -> list[str]:
        """Return names of all open tmux windows."""
        out = self._run_tmux("list-windows", "-t", self.SESSION, "-F", "#{window_name}")
        return out.splitlines() if out else []

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

    async def run_command(self, command: str, directory_path: str, pane_name: str = None, wait_secs: int = 2):
        """cd to directory_path and run command in the specified pane (default: shell)."""
        full_path = os.path.abspath(os.path.expanduser(directory_path))
        if not os.path.isdir(full_path):
            return f"Error: Directory '{directory_path}' does not exist."

        target = self._target(pane_name)
        full_cmd = f"cd '{full_path}' && {command}"
        self._run_tmux("send-keys", "-t", target, "")        # Escape
        self._run_tmux("send-keys", "-t", target, "C-u")
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "-l", full_cmd)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")

        await asyncio.sleep(wait_secs)
        return self.capture_output(pane_name=pane_name)

    async def send_input(self, text: str, pane_name: str = None):
        """Send raw text to whatever is currently running in the specified pane."""
        target = self._target(pane_name)
        self._run_tmux("send-keys", "-t", target, "-l", text)
        await asyncio.sleep(0.15)
        self._run_tmux("send-keys", "-t", target, "C-m")
        await asyncio.sleep(3)
        return self.capture_output(pane_name=pane_name)

    def capture_output(self, lines: int = 50, pane_name: str = None):
        """Capture recent terminal output from the specified pane."""
        result = subprocess.run(["tmux", "has-session", "-t", self.SESSION], capture_output=True)
        if result.returncode != 0:
            return "Error: Terminal session not running."
        target = self._target(pane_name)
        return self._run_tmux("capture-pane", "-p", "-t", target, "-S", f"-{lines}")

    def cleanup(self):
        logger.info(f"Killing tmux session: {self.SESSION}")
        subprocess.run(["tmux", "kill-session", "-t", self.SESSION], capture_output=True)
