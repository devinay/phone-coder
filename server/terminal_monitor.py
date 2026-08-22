"""Background trigger for the terminal pane.

This has a clock but no hands. It notices when the terminal is waiting on
something or has finished, and wakes the LLM. It never presses a key.

That division is deliberate. It used to decide for itself — regex-matching a
prompt, regex-matching a danger list, and pressing "1" — which meant a standing
instruction like "accept defaults, but pick the always-allow option when it is
offered" was understood by the model and then contradicted by this loop. Option
ordering is not even stable between Claude Code's dialogs, so no fixed key is
right. Choosing requires reading the screen and judging, which is the model's
job, and the model only acts on a turn.

So: decisions happen at turn boundaries, where the user's own words are in the
prompt (see ``terminal_tasks``). This exists only for the case a turn boundary
cannot cover — the user has gone quiet, and something is waiting.
"""

import asyncio
import re
from dataclasses import dataclass
from enum import Enum

from loguru import logger
from pipecat.frames.frames import LLMRunFrame

from terminal_screen import normalise, same_screen, strip_borders

# A confirmation prompt worth waking for. Broader than the old set, because
# nothing is auto-answered off the back of it any more — a false positive now
# costs one look by the model, not a keypress into the wrong window.
_PROMPT_PATTERNS = [
    re.compile(r"\bdo you want to\b", re.I),
    re.compile(r"\bproceed\b\??\s*$", re.I | re.M),
    re.compile(r"\(y(es)?/n(o)?\)", re.I),
    re.compile(r"\[y/n\]", re.I),
    re.compile(r"\bcontinue\?\s*$", re.I | re.M),
    re.compile(r"\bconfirm\b.*\?", re.I),
    re.compile(r"❯\s*\d+\.\s", re.I),        # any numbered selector
    re.compile(r"^\s*\d+\.\s+(yes|no|allow|deny|skip)\b", re.I | re.M),
    re.compile(r"\bpress\s+(enter|any key)\b", re.I),
    re.compile(r"\bwaiting for\b.*\binput\b", re.I),
]

# Shell prompt at the end of the screen: the task really has finished, rather
# than the program merely being quiet while it thinks.
_SHELL_PROMPT = re.compile(r"[>$#❯]\s*$")

# How many trailing lines count as "live". A TUI dialog is a bordered box whose
# question sits several lines above the last option, so 3 lines (the original
# window) could never see it. It stays deliberately small: widen this far enough
# and a question that already scrolled past looks live again.
_PROMPT_WINDOW = 15

# How long the user must have been quiet before this interrupts them. While they
# are talking, the turn boundary will service the terminal anyway, so waking is
# both unnecessary and rude.
_QUIET_BEFORE_WAKE_SECS = 15.0


class WakeReason(str, Enum):
    """Why the LLM was woken. Reported so the model knows what to do."""

    PROMPT = "prompt"       # something is waiting for input
    FINISHED = "finished"   # the work completed
    PATTERN = "pattern"     # a pattern the user asked about appeared
    EXPIRED = "expired"     # the watch ran out of time


@dataclass
class MonitorState:
    running: bool = False
    checks: int = 0
    wakes: int = 0
    awaiting_user: bool = False


def find_prompt(screen: str, alternate_screen: bool = False) -> str:
    """Return the question if the terminal is waiting on one, else "".

    Stricter than "does the screen contain a question". A shell prompt at the
    end means nothing is waiting — the question on screen was already answered.
    And the question has to be near the bottom: text scrolled further up is
    history, not a live prompt.

    Box-drawing characters are stripped before matching, because a full-screen
    program draws its question inside a border and the border otherwise hides
    the text from every pattern.
    """
    stripped = screen.rstrip()
    if not stripped or is_finished(stripped, alternate_screen):
        return ""
    lines = [
        cleaned
        for ln in stripped.splitlines()
        if (cleaned := strip_borders(ln).strip())
    ]
    if not lines:
        return ""
    tail = "\n".join(lines[-_PROMPT_WINDOW:])
    for pattern in _PROMPT_PATTERNS:
        if pattern.search(tail):
            return tail
    return ""


def is_finished(screen: str, alternate_screen: bool = False) -> bool:
    """Whether the pane is back at a shell prompt with nothing running.

    While a full-screen program owns the pane nothing has finished, whatever the
    last line looks like — Claude Code's own input box ends in ``>``, which the
    shell-prompt pattern would otherwise read as "back at the shell".
    """
    if alternate_screen:
        return False
    stripped = screen.rstrip()
    return bool(stripped) and bool(_SHELL_PROMPT.search(stripped.splitlines()[-1]))


class TerminalMonitor:
    """Watches the pane and wakes the LLM. Decides nothing, presses nothing."""

    def __init__(self, router, task, context):
        self._router = router
        self._task = task
        self._context = context
        self._loop_task: asyncio.Task | None = None
        self.state = MonitorState()
        # Normalised screen the model was last woken about, so a prompt that
        # simply stays on screen does not wake it again every tick.
        self._last_woken_for = ""

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, interval_secs: float = 2.0, max_minutes: float = 30.0) -> str:
        """Begin watching. The instruction itself lives in the watch registry."""
        if self.state.running:
            return "Already watching the terminal."
        self.state = MonitorState(running=True)
        self._last_woken_for = ""
        self._loop_task = asyncio.ensure_future(self._run(interval_secs, max_minutes))
        logger.info(
            f"[MONITOR] started interval={interval_secs}s max_minutes={max_minutes} "
            "(trigger-only: wakes the LLM, never sends keys)"
        )
        return "Watching the terminal."

    def stop(self, reason: str = "asked to stop") -> str:
        if not self.state.running:
            return "Not watching the terminal."
        self.state.running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        logger.info(
            f"[MONITOR] stopped ({reason}) after {self.state.checks} checks, "
            f"{self.state.wakes} wake(s)"
        )
        return f"Stopped watching the terminal ({reason})."

    # ── internals ────────────────────────────────────────────────────────────

    async def _wake(self, reason: WakeReason, screen: str) -> None:
        """Hand the situation to the LLM, with no opinion about what to do.

        The standing instruction is already in the system prompt via the watch
        registry, so this deliberately does not restate or interpret it.
        """
        self.state.wakes += 1
        headline = {
            WakeReason.PROMPT: (
                "The terminal is waiting for input. Decide what to do using the "
                "standing watch instruction, act on it, then tell the user briefly "
                "what you did."
            ),
            WakeReason.FINISHED: (
                "The command has finished and the terminal is back at a prompt. "
                "Summarise the outcome for the user in one or two sentences."
            ),
            WakeReason.PATTERN: (
                "The pattern the user asked to watch for has appeared. Tell them, "
                "and act if the standing instruction covers it."
            ),
            WakeReason.EXPIRED: (
                "The watch has run out of time and has stopped. Tell the user "
                "briefly, and mention they can ask to watch again."
            ),
        }[reason]
        self._context.add_message(
            {
                "role": "user",
                "content": f"[TERMINAL MONITOR] {headline}\n\nScreen:\n\n{screen[-2000:]}",
            }
        )
        await self._task.queue_frames([LLMRunFrame()])
        logger.info(f"[MONITOR] woke the model ({reason.value})")

    def _user_is_quiet(self) -> bool:
        """Whether the user has been silent long enough to interrupt.

        While they are talking there is no need: the next turn boundary services
        the terminal anyway.
        """
        idle = getattr(self._router, "seconds_since_user_spoke", None)
        if idle is None:
            return True
        return idle() >= _QUIET_BEFORE_WAKE_SECS

    async def _run(self, interval_secs: float, max_minutes: float) -> None:
        deadline = asyncio.get_event_loop().time() + max_minutes * 60
        # "Finished" only means something after something started. Monitoring is
        # often requested while the terminal already sits at a prompt, so the
        # first settled poll becomes the baseline rather than a completion.
        settled: str | None = None
        try:
            while self.state.running:
                await asyncio.sleep(interval_secs)
                self.state.checks += 1

                screen = self._router.capture_output()
                alt = self._router.on_alternate_screen()
                watches = getattr(self._router, "watches", None)

                # A pattern the user named takes priority over everything.
                pattern = next(
                    (t.watch_for for t in (watches.active if watches else []) if t.watch_for),
                    "",
                )
                if pattern and self._matches(pattern, screen):
                    await self._wake(WakeReason.PATTERN, screen)
                    self.stop("pattern matched")
                    return

                prompt = find_prompt(screen, alt)
                if prompt and self._user_is_quiet():
                    marker = normalise(prompt)
                    if marker != self._last_woken_for:
                        self._last_woken_for = marker
                        await self._wake(WakeReason.PROMPT, screen)
                    continue

                if not prompt:
                    # The question went away, so a future identical one is new.
                    self._last_woken_for = ""

                if settled is None:
                    settled = screen
                    continue

                if (
                    not prompt
                    and not same_screen(screen, settled)
                    and is_finished(screen, alt)
                    and self._user_is_quiet()
                ):
                    await self._wake(WakeReason.FINISHED, screen)
                    self.stop("work finished")
                    return

                if asyncio.get_event_loop().time() > deadline:
                    await self._wake(WakeReason.EXPIRED, screen)
                    self.stop("time limit")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[MONITOR] loop failed: {e}")
            self.state.running = False

    @staticmethod
    def _matches(pattern: str, screen: str) -> bool:
        try:
            matcher = re.compile(pattern, re.I)
        except re.error:
            return False
        return bool(matcher.search(screen) or matcher.search(strip_borders(screen)))
