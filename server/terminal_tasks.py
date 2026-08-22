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

import time
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class WatchTask:
    """One standing instruction about the terminal."""

    instruction: str
    watch_for: str = ""
    started_at: float = field(default_factory=time.monotonic)
    max_minutes: float = 30.0
    acted: int = 0
    max_actions: int = 20
    last_report: str = ""

    @property
    def age_secs(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def expired(self) -> bool:
        return self.age_secs > self.max_minutes * 60

    @property
    def exhausted(self) -> bool:
        return self.acted >= self.max_actions

    def describe(self) -> str:
        age = self.age_secs
        when = f"{int(age)}s" if age < 60 else f"{int(age // 60)}m"
        bits = [f"started {when} ago"]
        if self.acted:
            bits.append(f"{self.acted} action(s) taken")
        if self.watch_for:
            bits.append(f"watching for {self.watch_for!r}")
        return f'"{self.instruction}" ({", ".join(bits)})'


class WatchRegistry:
    """The standing instructions currently in force."""

    def __init__(self):
        self._tasks: list[WatchTask] = []

    def add(
        self,
        instruction: str,
        watch_for: str = "",
        max_minutes: float = 30.0,
        max_actions: int = 20,
    ) -> WatchTask:
        task = WatchTask(
            instruction=instruction.strip(),
            watch_for=watch_for,
            max_minutes=max_minutes,
            max_actions=max_actions,
        )
        self._tasks.append(task)
        logger.info(
            f"[WATCH] added {task.instruction!r} watch_for={watch_for!r} "
            f"max_minutes={max_minutes} max_actions={max_actions}"
        )
        return task

    def clear(self) -> int:
        count = len(self._tasks)
        self._tasks = []
        if count:
            logger.info(f"[WATCH] cleared {count} task(s)")
        return count

    def prune(self) -> list[WatchTask]:
        """Drop tasks that have run out of time or actions; return what went."""
        keep, dropped = [], []
        for task in self._tasks:
            (dropped if (task.expired or task.exhausted) else keep).append(task)
        if dropped:
            self._tasks = keep
            for task in dropped:
                why = "time limit" if task.expired else "action limit"
                logger.info(f"[WATCH] dropped {task.instruction!r} ({why})")
        return dropped

    def note_action(self) -> None:
        """Record that the agent acted on the terminal under these instructions."""
        for task in self._tasks:
            task.acted += 1

    @property
    def active(self) -> list[WatchTask]:
        return list(self._tasks)

    def __bool__(self) -> bool:
        return bool(self._tasks)

    def describe(self) -> str:
        """The block injected into the prompt, or "" when nothing is watched."""
        if not self._tasks:
            return ""
        if len(self._tasks) == 1:
            return f"[WATCHING] {self._tasks[0].describe()}"
        lines = [f"[WATCHING] {len(self._tasks)} standing instructions:"]
        lines += [f"  - {task.describe()}" for task in self._tasks]
        return "\n".join(lines)
