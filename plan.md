# Voice Coding Cockpit - Phase 1 Plan (Dynamic Orchestration)

## 1. Proposed Architecture

The system uses a **voice-driven and text-driven conversational AI** as a top-level controller/router. This removes the need for custom UI buttons and static startup scripts, giving you a dynamic, highly scalable workspace.

*   **Browser UI:** The existing Pipecat Prebuilt WebRTC UI will be used. It handles voice input/output and provides a text chat interface (useful for typing verbatim commands).
*   **Controller Brain (`bot.py`):** The existing `OpenAILLMService` acts as the top-level orchestrator. It holds conversational context and decides when to answer directly or when to invoke system tools.
*   **Agent Router (`agent_router.py`):** A new Python module bridging the Python server and `tmux`. It dynamically spins up sessions and windows on demand, changes directories, and executes commands.
*   **Dynamic Local Agents:** Instead of starting all agents at boot, the LLM will launch Claude Code, Codex, or Gemini into specific repository paths on your command.

*Compatibility Note for Phase 2 (Tailscale):* Because the WebRTC UI and server remain standard, exposing the local Pipecat server over Tailscale will work out of the box without changing this architecture.

## 2. Controller Toolset (LLM Capabilities)

The Top-Level AI will be equipped with the following functional tools:

1.  `launch_assistant(assistant_name, directory_path)`: Creates a new tmux window named `assistant_name`, navigates to `directory_path`, and starts the assistant CLI.
2.  `send_message(assistant_name, message)`: Sends a natural language instruction or query to a running assistant via `tmux send-keys`.
3.  `run_shell_command(command, directory_path)`: Creates or uses a general `shell` tmux window, navigates to the directory, executes a verbatim command (e.g., `git status`, `npm run build`), and returns the immediate output.
4.  `capture_output(window_name)`: Captures the recent history of a specified tmux window (assistant or shell) so the LLM can read the results, summarize them, and respond to the user.

## 3. Minimal Code Changes

### A. Create `server/agent_router.py`
A robust wrapper around `tmux` for dynamic management:
```python
import subprocess

class AgentRouter:
    def __init__(self, session_name="cockpit"):
        self.session_name = session_name
        self._ensure_session()

    def _ensure_session(self):
        # Create session if it doesn't exist; start with a base window
        subprocess.run(["tmux", "has-session", "-t", self.session_name], capture_output=True)
        if subprocess.run(["tmux", "has-session", "-t", self.session_name]).returncode != 0:
            subprocess.run(["tmux", "new-session", "-d", "-s", self.session_name, "-n", "base"])

    def _run_tmux(self, *args):
        cmd = ["tmux"] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip()
        
    def launch_assistant(self, assistant_name: str, directory_path: str):
        # Create a new window, cd into directory, and start the CLI
        self._run_tmux("new-window", "-t", self.session_name, "-n", assistant_name)
        target = f"{self.session_name}:{assistant_name}"
        self._run_tmux("send-keys", "-t", target, f"cd {directory_path} && {assistant_name}", "Enter")
        return f"Launched {assistant_name} in {directory_path}."

    def send_message(self, assistant_name: str, message: str):
        target = f"{self.session_name}:{assistant_name}"
        self._run_tmux("send-keys", "-t", target, message, "Enter")
        return f"Message sent to {assistant_name}."

    def run_shell_command(self, command: str, directory_path: str):
        # Use or create a 'shell' window
        target = f"{self.session_name}:shell"
        self._run_tmux("new-window", "-t", self.session_name, "-n", "shell") # Fails safely if exists
        self._run_tmux("send-keys", "-t", target, f"cd {directory_path} && {command}", "Enter")
        return f"Executed '{command}' in {directory_path}. Use capture_output('shell') to view results if needed."

    def capture_output(self, window_name: str, lines: int = 50):
        target = f"{self.session_name}:{window_name}"
        return self._run_tmux("capture-pane", "-p", "-t", target, "-S", f"-{lines}")
```

### B. Update `server/bot.py`
1.  Import and instantiate `AgentRouter`.
2.  Use Pipecat's `@llm.function` (or standard OpenAI function definitions depending on the Pipecat version installed) to expose the four methods above to the `OpenAILLMService`.
3.  Rewrite the `LLMContext` system prompt. Example: *"You are the top-level orchestration AI for a local voice coding cockpit. The user will ask you to manage coding assistants (Claude, Codex, Gemini) or run shell commands. You do not write code directly. You use your tools to launch assistants in specific directories, send them messages, execute verbatim shell commands, and capture their output to summarize back to the user."*

## 4. Initialization
No hardcoded `.sh` bash scripts are required.
You just start the server: `uv run bot.py`. The `AgentRouter` will transparently create the `cockpit` tmux session in the background when the first tool is called.

## 5. Local Test Checklist

1.  [ ] **Start System:** Run `uv run bot.py` and open the local Pipecat WebRTC UI in your browser.
2.  [ ] **Test Dynamic Launch:** Speak/Type: *"Start claude in the directory /Users/mridula/src/pipecat/pipecat-quickstart."*
3.  [ ] **Test Verbatim Shell:** Speak/Type: *"Run the shell command `ls -la` in that same directory."*
4.  [ ] **Test Output Capture:** Speak/Type: *"What did the shell command output?"* (LLM should read the shell pane).
5.  [ ] **Test Agent Routing:** Speak/Type: *"Tell claude to look at bot.py."*
6.  [ ] **Verify Tmux:** Open a terminal and run `tmux attach -t cockpit` to visually confirm the windows and commands.
## Implemented (Phase 1)

- [x] **Agent Router (server/agent_router.py):** Robust tmux wrapper for session/window management.
- [x] **Orchestration Tools:** Integrated launch_assistant, send_message, run_shell_command, and capture_output into bot.py.
- [x] **Pipecat 1.1.0 Integration:** Used DirectFunction and ToolsSchema for modern tool-calling support.
- [x] **System Prompt Update:** Reconfigured LLM as a top-level orchestrator.
- [x] **Verification:** Confirmed tmux session lifecycle and bot initialization.
- [x] **Intelligent Directory Discovery:** Implemented 3-level deep recursive search for partial directory names.
- [x] **Fuzzy Matching:** Added support for similar-sounding directory names to handle STT errors.
- [x] **Path Expansion:** Support for \`~\` and absolute path resolution.
- [x] **Session Cleanup:** Tmux session and agents now automatically exit when the bot disconnects.
- [x] **Command Execution Fix:** Ensured all tmux commands are followed by an explicit "Enter" key press.
- [x] **Robust Window Targeting:** Switched to internal tmux window IDs to prevent "can't find window" errors during renaming.
