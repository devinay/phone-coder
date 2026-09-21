"""Standing instructions about the terminal, kept where they cannot be forgotten.

"Watch claude and accept defaults, but if there's an always-allow option pick
that" is a policy no enum can express and no regex can apply. It has to be read
and judged against whatever is actually on screen, which means an LLM has to do
it — and an LLM only ever acts on a turn.

So the instruction is stored here verbatim, rendered into the prompt every turn,
and applied at turn boundaries. It lives on the router rather than in the
message list for the same reason the foreground stack does: tool results and
messages get stripped on agent handoff, and a standing instruction that
evaporates when the user changes the subject is worse than none.

Nothing in this module decides anything. It remembers what was asked.
"""

import itertools
import time
from dataclasses import dataclass, field

from loguru import logger

# Instructions carry a target so "watch claude, and also tell me when the build
# breaks" is two instructions against two panes rather than one that silently
# replaces the other. An empty target means the router's default pane, resolved
# by the caller, so nothing here needs to know tmux's naming.
DEFAULT_TARGET = ""

_ids = itertools.count(1)


@dataclass
class WatchTask:
    """One standing instruction about the terminal."""

    instruction: str
    watch_for: str = ""
    target: str = DEFAULT_TARGET
    started_at: float = field(default_factory=time.monotonic)
    max_minutes: float = 30.0
    acted: int = 0
    max_actions: int = 20
    last_report: str = ""
    id: int = field(default_factory=lambda: next(_ids))

    @property
    def age_secs(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def expired(self) -> bool:
        return self.age_secs > self.max_minutes * 60

    @property
    def exhausted(self) -> bool:
        return self.acted >= self.max_actions

    def describe(self, show_target: bool = False) -> str:
        age = self.age_secs
        when = f"{int(age)}s" if age < 60 else f"{int(age // 60)}m"
        bits = [f"started {when} ago"]
        if self.acted:
            bits.append(f"{self.acted} action(s) taken")
        if self.watch_for:
            bits.append(f"watching for {self.watch_for!r}")
        if show_target:
            bits.append(f"pane {self.target or 'default'}")
        return f'#{self.id} "{self.instruction}" ({", ".join(bits)})'


class WatchRegistry:
    """The standing instructions currently in force, across every watched pane."""

    def __init__(self):
        self._tasks: list[WatchTask] = []

    def add(
        self,
        instruction: str,
        watch_for: str = "",
        target: str = DEFAULT_TARGET,
        max_minutes: float = 30.0,
        max_actions: int = 20,
    ) -> WatchTask:
        task = WatchTask(
            instruction=instruction.strip(),
            watch_for=watch_for,
            target=target,
            max_minutes=max_minutes,
            max_actions=max_actions,
        )
        self._tasks.append(task)
        logger.info(
            f"[WATCH] added #{task.id} {task.instruction!r} watch_for={watch_for!r} "
            f"target={target or 'default'} max_minutes={max_minutes} max_actions={max_actions}"
        )
        return task

    def clear(self, target: str | None = None) -> int:
        """Drop every instruction, or only those for one pane."""
        if target is None:
            count = len(self._tasks)
            self._tasks = []
        else:
            keep = [t for t in self._tasks if t.target != target]
            count = len(self._tasks) - len(keep)
            self._tasks = keep
        if count:
            logger.info(f"[WATCH] cleared {count} task(s) target={target or 'all'}")
        return count

    def drop(self, task: WatchTask, why: str = "done") -> bool:
        """Retire one instruction, leaving any others on the same pane running."""
        if task not in self._tasks:
            return False
        self._tasks.remove(task)
        logger.info(f"[WATCH] dropped #{task.id} {task.instruction!r} ({why})")
        return True

    def prune(self) -> list[WatchTask]:
        """Drop tasks that have run out of time or actions; return what went."""
        keep, dropped = [], []
        for task in self._tasks:
            (dropped if (task.expired or task.exhausted) else keep).append(task)
        if dropped:
            self._tasks = keep
            for task in dropped:
                why = "time limit" if task.expired else "action limit"
                logger.info(f"[WATCH] dropped #{task.id} {task.instruction!r} ({why})")
        return dropped

    def note_action(self, target: str | None = None) -> None:
        """Record that the agent acted on the terminal under these instructions.

        Charged to the pane that was acted on. Charging every instruction meant
        answering Claude Code's prompts also burned down the budget of an
        unrelated "tell me when the build finishes" watch on another pane.
        """
        for task in self._tasks:
            if target is None or task.target == target:
                task.acted += 1

    def for_target(self, target: str) -> list[WatchTask]:
        return [t for t in self._tasks if t.target == target]

    def targets(self) -> list[str]:
        """Every watched pane, in the order it was first watched."""
        seen: list[str] = []
        for task in self._tasks:
            if task.target not in seen:
                seen.append(task.target)
        return seen

    @property
    def active(self) -> list[WatchTask]:
        return list(self._tasks)

    def __bool__(self) -> bool:
        return bool(self._tasks)

    def describe(self) -> str:
        """The block injected into the prompt, or "" when nothing is watched."""
        if not self._tasks:
            return ""
        # The pane is only worth naming once more than one is in play; with a
        # single pane it is noise in every prompt.
        show_target = len(self.targets()) > 1
        if len(self._tasks) == 1:
            return f"[WATCHING] {self._tasks[0].describe(show_target)}"
        lines = [f"[WATCHING] {len(self._tasks)} standing instructions:"]
        lines += [f"  - {task.describe(show_target)}" for task in self._tasks]
        return "\n".join(lines)
