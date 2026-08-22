"""What is running in the terminal right now, as a stack.

Every agent used to learn this from a tool result — and tool results are
stripped from context on every agent handoff, so the fact that Claude Code was
launched survived exactly until the next switch. The controller then had no way
to know a coding agent was running, and would offer to launch one again.

Holding it here instead means it lives outside the message list and is rendered
into the prompt on every turn, so it cannot be forgotten.

A stack rather than a flag because launches nest: fish runs claude, claude runs
a build. When the inner thing exits, attention returns to the outer one.
"""

import time
from dataclasses import dataclass, field

from loguru import logger

# Programs that take over the pane and keep running until told to stop. Anything
# else is treated as a command that runs and exits.
INTERACTIVE = {
    "claude", "codex", "cursor", "aider", "gemini",
    "vim", "nvim", "vi", "emacs", "nano",
    "less", "more", "man", "top", "htop", "btop",
    "python", "python3", "ipython", "node", "irb", "psql", "mongosh", "fzf",
    "ssh", "watch",
}
# Deliberately absent: git. `git status` and friends exit immediately, and they
# are among the commonest commands here. A wrong guess self-heals on the next
# reconcile, but not before one turn has reported a program that already exited.

# Shells: the resting state. The stack is empty when one of these is foreground.
SHELLS = {"fish", "bash", "zsh", "sh", "dash", "ksh"}


@dataclass
class Program:
    """One thing launched into the pane."""

    name: str
    command: str
    cwd: str = ""
    started_at: float = field(default_factory=time.monotonic)
    full_screen: bool = False

    @property
    def age_secs(self) -> float:
        return time.monotonic() - self.started_at

    def describe(self) -> str:
        age = self.age_secs
        if age < 60:
            when = f"{int(age)}s"
        elif age < 3600:
            when = f"{int(age // 60)}m"
        else:
            when = f"{age / 3600:.1f}h"
        bits = [self.name, f"running {when}"]
        if self.full_screen:
            bits.append("full-screen")
        return f"{bits[0]} ({', '.join(bits[1:])})"


def program_name(command: str) -> str:
    """The program a shell command actually runs.

    Skips leading environment assignments and common wrappers so
    ``FOO=1 uv run claude`` is reported as ``claude`` rather than ``uv``.
    """
    wrappers = {"sudo", "doas", "env", "time", "nohup", "uv", "npx", "poetry", "pdm", "rye"}
    subcommands = {"run", "exec", "tool"}
    for token in command.split():
        if "=" in token and not token.startswith("-"):
            continue  # VAR=value
        if token.startswith("-"):
            continue
        base = token.split("/")[-1]
        if base in wrappers or base in subcommands:
            continue
        return base
    return ""


def is_interactive(command: str) -> bool:
    """Whether this command is expected to stay in the foreground."""
    return program_name(command) in INTERACTIVE


class ForegroundStack:
    """Tracks what owns the pane, reconciled against tmux.

    The stack records what *we* launched; tmux is the authority on whether it is
    still there. Both are needed: tmux alone cannot say what a process is for,
    and the stack alone would never notice a program the user quit by hand.
    """

    def __init__(self):
        self._stack: list[Program] = []
        self._shell = "fish"

    # ── mutation ─────────────────────────────────────────────────────────────

    def push(self, command: str, cwd: str = "", full_screen: bool = False) -> None:
        name = program_name(command) or command.strip()[:20]
        entry = Program(name=name, command=command, cwd=cwd, full_screen=full_screen)
        self._stack.append(entry)
        logger.info(f"[FOREGROUND] pushed {name!r} depth={len(self._stack)} cmd={command!r}")

    def reconcile(self, pane_command: str, alternate_on: bool) -> list[Program]:
        """Align the stack with what tmux reports; return anything that exited.

        The subtlety that makes this worth a method: Claude Code spawns its own
        subprocesses for its bash tool, so ``pane_current_command`` will
        transiently report something that is neither claude nor the shell. That
        is not an exit. The stack only drains when the pane is genuinely back at
        a shell *and* no full-screen program holds the display — checking either
        signal alone pops Claude off every time it runs a tool.
        """
        if not pane_command:
            return []
        at_shell = pane_command in SHELLS
        if at_shell:
            self._shell = pane_command
        if not (at_shell and not alternate_on):
            # Something is still running; keep the stack and refresh the flag on
            # the top entry so "full-screen" stays accurate.
            if self._stack:
                self._stack[-1].full_screen = alternate_on
            return []
        if not self._stack:
            return []
        exited = self._stack
        self._stack = []
        logger.info(
            f"[FOREGROUND] drained {[p.name for p in exited]} "
            f"(pane back at {pane_command}, alternate_on=0)"
        )
        return exited

    def clear(self) -> None:
        self._stack = []

    # ── read ─────────────────────────────────────────────────────────────────

    @property
    def current(self) -> Program | None:
        return self._stack[-1] if self._stack else None

    @property
    def depth(self) -> int:
        return len(self._stack)

    def is_running(self, name: str) -> bool:
        return any(p.name == name for p in self._stack)

    def describe(self, cwd: str = "") -> str:
        """One line for the prompt: what is running, where.

        Rendered every turn for every agent, so it stays short.
        """
        where = cwd or (self._stack[0].cwd if self._stack else "")
        head = f"{self._shell} {where}".strip()
        if not self._stack:
            return f"[TERMINAL] {head} — nothing running, shell is idle"
        chain = " › ".join(p.describe() for p in self._stack)
        return f"[TERMINAL] {head} › {chain}"
