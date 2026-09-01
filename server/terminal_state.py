"""What is running in the terminal right now, derived rather than remembered.

This module used to hold a ``ForegroundStack``: a push-down stack of programs we
had launched, reconciled against tmux on every turn. It had a structural bug.
The stack was only ever pushed to when *we* ran the command, so attaching to a
session where Claude Code was already running — the terminal closed, Claude kept
going, the terminal reopened — left the stack empty and the pane reported as an
idle shell. The controller would then type lines into a TUI that only reads
keypresses, and offer to launch an agent that was already there.

The fix is not to also push on attach. It is to stop storing the answer. The
kernel already knows what is running in a pane, so any cached copy can only
drift; the reconcile machinery existed purely to manage that drift.

Two readings replace it, and they answer genuinely different questions:

- **``ps -t <pane_tty>``** — identity and structure. What is running, how it
  nests, how long it has been up. Scoped by tty rather than by walking parent
  links, because a shell forks: fish appears twice before Claude shows up, so
  Claude is a *grandchild* of ``pane_pid`` and a children-of-pid lookup misses
  it entirely. One ``ps`` call needs no recursion and cannot miss depth.
- **the rendered screen** — state and intent. Whether the thing is blocked on a
  permission dialog, spinning, or sitting at a prompt. No process-level signal
  can tell you this; the process looks identical either way.

Correlating them is what lets the controller decide between ``send_key`` and
``send_input``, which is the decision that used to break. They are taken
together in one ``Observation`` so the pair is always internally consistent —
read separately, you can get "waiting for permission" against a process list
where the program has already exited.

Deliberately not stored here: anything the runtime can answer. Output *history*
is the exception and lives in ``terminal_history``, because tmux discards the
past and only a record can recover it.
"""

import re
import time
from dataclasses import dataclass, field

# Programs that take over the pane. Purely a display filter now: nothing has to
# predict whether a command will stick around, because we look instead of
# guessing. Used to decide what is worth *naming* in the status line.
INTERACTIVE = {
    "claude", "codex", "cursor", "aider", "gemini",
    "vim", "nvim", "vi", "emacs", "nano",
    "less", "more", "man", "top", "htop", "btop",
    "python", "python3", "ipython", "node", "irb", "psql", "mongosh", "fzf",
    "ssh", "watch",
}

# Shells: the resting state. Never named as "running"; they are the baseline.
SHELLS = {"fish", "bash", "zsh", "sh", "dash", "ksh"}

# Long-lived helpers that a coding agent spawns and keeps. These sit in the
# pane's foreground process group looking exactly like a program the user
# launched, so without this filter attaching to Claude Code reports
# "claude › python" — the python being one of its MCP servers.
BACKGROUND_HELPERS = {"caffeinate", "mcp", "mcp-server", "language-server"}
_HELPER_HINT = re.compile(r"(?:^|[-_/])(?:mcp|lsp|language.server|daemon)(?:[-_]|$)", re.I)


@dataclass(frozen=True)
class Process:
    """One process on the pane's tty."""

    pid: int
    ppid: int
    foreground: bool  # in the tty's foreground process group ('+' in STAT)
    age_secs: float
    name: str  # basename of comm
    raw: str = ""  # comm as ps reported it, path and all

    @property
    def is_shell(self) -> bool:
        return self.name in SHELLS

    @property
    def is_helper(self) -> bool:
        """A tool a program spawned for itself, not something owning the pane."""
        return self.name in BACKGROUND_HELPERS or bool(_HELPER_HINT.search(self.raw))


def format_age(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def program_name(command: str) -> str:
    """The program a command string actually names.

    Skips leading environment assignments and common wrappers so
    ``FOO=1 uv run claude`` reads as ``claude`` rather than ``uv``, and reduces
    a path to its basename so ``/opt/venv/bin/python`` reads as ``python``.
    Applied to ``ps`` output as well as to commands we are about to send.
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


def parse_etime(field_value: str) -> float:
    """Seconds from ps's elapsed-time format: ``[[dd-]hh:]mm:ss``.

    ``etime`` rather than ``lstart`` because it has no spaces, which keeps the
    whole ps line splittable on whitespace with the command last.
    """
    text = field_value.strip()
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return 0.0
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0.0
    while len(numbers) < 3:
        numbers.insert(0, 0)  # mm:ss → 0:mm:ss
    hours, minutes, seconds = numbers[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# The ps format this parser expects. Command last, because it is the only field
# that can contain spaces.
PS_FORMAT = "pid=,ppid=,stat=,etime=,comm="


def parse_ps(output: str) -> list[Process]:
    """Parse ``ps -t <tty> -o pid=,ppid=,stat=,etime=,comm=`` output."""
    processes = []
    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        pid, ppid, stat, etime, comm = fields
        try:
            pid_n, ppid_n = int(pid), int(ppid)
        except ValueError:
            continue  # a header row, or something unparseable
        processes.append(Process(
            pid=pid_n,
            ppid=ppid_n,
            # '+' marks the foreground process group. Note this does not on its
            # own identify the program that owns the pane: a coding agent and
            # every child it has spawned all carry it.
            foreground="+" in stat,
            age_secs=parse_etime(etime),
            name=program_name(comm) or comm.strip(),
            raw=comm.strip(),
        ))
    return processes


def pane_owner(processes: list[Process]) -> Process | None:
    """The program that owns the pane, or None if a shell is at rest.

    Among foreground processes, the owner is the *shallowest* non-shell one —
    the ancestor of the rest. Depth is counted within the tty set, so fish's
    double fork does not change the answer.

    Only the owner is returned, not the full chain, because deeper foreground
    processes are ambiguous: Claude Code's transient bash-tool child and its
    long-lived MCP servers are both children of claude and look alike. What the
    controller needs is which program is reading the keyboard, and that is the
    owner. ``Observation.busy_with`` carries the transient detail instead.
    """
    by_pid = {p.pid: p for p in processes}

    def depth(proc: Process) -> int:
        seen, n, cur = {proc.pid}, 0, proc
        while cur.ppid in by_pid and cur.ppid not in seen:
            seen.add(cur.ppid)
            cur = by_pid[cur.ppid]
            n += 1
        return n

    candidates = [
        p for p in processes
        if p.foreground and not p.is_shell and not p.is_helper
    ]
    if not candidates:
        return None
    return min(candidates, key=depth)


def current_shell(processes: list[Process], default: str = "fish") -> str:
    """The shell hosting the pane: the shallowest shell on the tty."""
    shells = [p for p in processes if p.is_shell]
    if not shells:
        return default
    return min(shells, key=lambda p: p.pid).name


@dataclass(frozen=True)
class Observation:
    """One correlated reading of the terminal, taken at a single moment.

    Timestamped as a whole so callers cannot pair a stale process list with a
    fresh screen, which is how a program that has already exited gets reported
    as blocked on a question.
    """

    at: float = field(default_factory=time.time)
    session_running: bool = True
    shell: str = "fish"
    cwd: str = ""
    owner: Process | None = None
    busy_with: str = ""  # tmux's pane_current_command, if it differs from owner
    full_screen: bool = False
    processes: list[Process] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        """Whether the pane is a shell at rest."""
        return self.owner is None

    def is_running(self, name: str) -> bool:
        return any(p.name == name and p.foreground for p in self.processes)

    def describe(self) -> str:
        """One line for the prompt, rendered every turn for every agent."""
        if not self.session_running:
            return "[TERMINAL] no session running"
        head = f"{self.shell} {self.cwd}".strip()
        if self.owner is None:
            return f"[TERMINAL] {head} — nothing running, shell is idle"
        bits = [f"running {format_age(self.owner.age_secs)}"]
        if self.full_screen:
            bits.append("full-screen")
        if self.busy_with and self.busy_with != self.owner.name:
            bits.append(f"currently busy in {self.busy_with}")
        return f"[TERMINAL] {head} › {self.owner.name} ({', '.join(bits)})"
