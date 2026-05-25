import subprocess
import os
import difflib
import asyncio
from loguru import logger

class AgentRouter:
    def __init__(self, session_name="cockpit"):
        self.session_name = session_name
        self._ensure_session()

    def _ensure_session(self):
        # Create session if it doesn't exist; start with a base window
        result = subprocess.run(["tmux", "has-session", "-t", self.session_name], capture_output=True)
        if result.returncode != 0:
            logger.info(f"Creating new tmux session: {self.session_name}")
            subprocess.run(["tmux", "new-session", "-d", "-s", self.session_name, "-n", "base"])
        else:
            logger.info(f"Tmux session {self.session_name} already exists")
        
        # Disable window renaming globally for this session to ensure our names stick
        subprocess.run(["tmux", "set-option", "-t", self.session_name, "allow-rename", "off"])

    def _run_tmux(self, *args):
        cmd = ["tmux"] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Tmux command failed: {' '.join(cmd)} - {result.stderr}")
        return result.stdout.strip()

    def find_best_directory(self, path_or_name: str, base_dir: str = None):
        """Attempts to find the best directory match using exact and fuzzy searching."""
        full_path = os.path.abspath(os.path.expanduser(path_or_name))
        if os.path.isdir(full_path):
            return full_path, True

        if not base_dir:
            base_dir = os.path.abspath(os.path.expanduser("~"))
        
        matches = []
        all_dirs = {}
        target_name = os.path.basename(path_or_name).lower()
        exclude_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__", "Library"}
        
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            depth = root[len(base_dir):].count(os.sep)
            if depth >= 3:
                dirs[:] = [] 
                continue
            
            for d in dirs:
                d_lower = d.lower()
                d_path = os.path.join(root, d)
                if d_lower not in all_dirs:
                    all_dirs[d_lower] = []
                all_dirs[d_lower].append(d_path)
                if d_lower == target_name:
                    matches.append(d_path)
        
        if len(matches) == 1:
            return matches[0], False
        elif len(matches) > 1:
            return matches, False
            
        similar_names = difflib.get_close_matches(target_name, all_dirs.keys(), n=3, cutoff=0.6)
        if similar_names:
            fuzzy_matches = []
            for name in similar_names:
                fuzzy_matches.extend(all_dirs[name])
            return fuzzy_matches, False
        return None, False

    async def launch_assistant(self, assistant_name: str, directory_path: str):
        full_path = os.path.abspath(os.path.expanduser(directory_path))
        if not os.path.isdir(full_path):
            return f"Error: Directory '{directory_path}' (resolved to '{full_path}') does not exist."

        cli_commands = {"claude": "claude", "codex": "codex", "gemini": "gemini"}
        cmd = cli_commands.get(assistant_name.lower(), assistant_name)

        check_cmd = subprocess.run(["which", cmd.split()[0]], capture_output=True, text=True)
        if check_cmd.returncode != 0:
            return f"Error: Command '{cmd}' (for {assistant_name}) not found on path."

        target = f"{self.session_name}:{assistant_name}"
        result = subprocess.run(["tmux", "select-window", "-t", target], capture_output=True)
        if result.returncode != 0:
            self._run_tmux("new-window", "-d", "-t", self.session_name, "-n", assistant_name)
            self._run_tmux("set-window-option", "-t", target, "allow-rename", "off")
        
        self._run_tmux("send-keys", "-t", target, "Escape")
        self._run_tmux("send-keys", "-t", target, "C-u")
        await asyncio.sleep(0.2)
        self._run_tmux("send-keys", "-t", target, "-l", f"cd '{full_path}' && {cmd}")
        await asyncio.sleep(0.2)
        self._run_tmux("send-keys", "-t", target, "C-m")
        
        return f"Launched {assistant_name} in {full_path}. Ask to 'capture output' if you don't see results."

    async def send_message(self, assistant_name: str, message: str, wait_secs: int = 5):
        target = f"{self.session_name}:{assistant_name}"
        result = subprocess.run(["tmux", "select-window", "-t", target], capture_output=True)
        if result.returncode != 0:
            return f"Error: Assistant '{assistant_name}' is not running."

        self._run_tmux("send-keys", "-t", target, "Escape")
        self._run_tmux("send-keys", "-t", target, "C-u")
        await asyncio.sleep(0.2)
        self._run_tmux("send-keys", "-t", target, "-l", message.strip())
        await asyncio.sleep(0.2)
        self._run_tmux("send-keys", "-t", target, "C-m")
        
        await asyncio.sleep(wait_secs)
        output = self.capture_output(assistant_name, lines=25)
        return f"Message sent to {assistant_name}. Terminal output:\n{output}"

    async def run_shell_command(self, command: str, directory_path: str, wait_secs: int = 2):
        full_path = os.path.abspath(os.path.expanduser(directory_path))
        if not os.path.isdir(full_path):
            return f"Error: Directory '{directory_path}' does not exist."

        target = f"{self.session_name}:shell"
        result = subprocess.run(["tmux", "select-window", "-t", target], capture_output=True)
        if result.returncode != 0:
            self._run_tmux("new-window", "-t", self.session_name, "-n", "shell")
        
        self._run_tmux("send-keys", "-t", target, "Escape")
        self._run_tmux("send-keys", "-t", target, "C-u")
        await asyncio.sleep(0.2)
        self._run_tmux("send-keys", "-t", target, "-l", f"cd '{full_path}' && {command}")
        await asyncio.sleep(0.2)
        self._run_tmux("send-keys", "-t", target, "C-m")
        
        await asyncio.sleep(wait_secs)
        output = self.capture_output("shell", lines=20)
        return f"Executed '{command}' in {full_path}. Output:\n{output}"

    def capture_output(self, window_name: str, lines: int = 100):
        target = f"{self.session_name}:{window_name}"
        result = subprocess.run(["tmux", "select-window", "-t", target], capture_output=True)
        if result.returncode != 0:
            return f"Error: Window '{window_name}' not found."
        return self._run_tmux("capture-pane", "-p", "-t", target, "-S", f"-{lines}")

    def cleanup(self):
        logger.info(f"Cleaning up tmux session: {self.session_name}")
        subprocess.run(["tmux", "kill-session", "-t", self.session_name], capture_output=True)
