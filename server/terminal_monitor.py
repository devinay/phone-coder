"""Background watcher for the terminal pane.

Everything else in the cockpit is turn-scoped: a tool runs, returns, the turn
ends. This runs between turns, so it is the one component that can speak
without being spoken to — and, under an explicit policy, answer a confirmation
prompt on the user's behalf.

The loop itself is cheap: it polls the pane and only wakes the LLM when a
decision is needed or the work has finished. Waking on every change would cost
an LLM turn per tick and narrate noise.
"""

import asyncio
import re
from dataclasses import dataclass
from enum import Enum

from loguru import logger
from pipecat.frames.frames import LLMRunFrame

# A confirmation prompt worth acting on. Deliberately narrow: anything not
# recognised here is escalated rather than guessed at.
_PROMPT_PATTERNS = [
    re.compile(r"\bdo you want to\b", re.I),
    re.compile(r"\bproceed\b\??\s*$", re.I | re.M),
    re.compile(r"\(y(es)?/n(o)?\)", re.I),
    re.compile(r"\[y/n\]", re.I),
    re.compile(r"\bcontinue\?\s*$", re.I | re.M),
    re.compile(r"\bconfirm\b.*\?", re.I),
    re.compile(r"❯\s*1\.\s*yes", re.I),  # Claude Code's numbered menu
]

# Actions never auto-answered, whatever the policy short of blanket yes. These
# are irreversible, reach outside the working tree, or change access.
_DANGER_PATTERNS = [
    re.compile(r"\brm\s+-[rf]", re.I),
    re.compile(r"\bgit\s+(reset\s+--hard|push\s+(-f|--force)|rebase|filter-branch)", re.I),
    re.compile(r"\bforce[- ]push", re.I),
    re.compile(r"\b(drop|truncate)\s+(table|database)\b", re.I),
    re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777", re.I),
    re.compile(r"\b(sudo|doas)\b", re.I),
    re.compile(r"\b(npm|pip|uv|brew|apt|cargo)\s+(install|add|publish)", re.I),
    re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh", re.I),
    re.compile(r"\bssh\b|\bscp\b|\bkubectl\b|\bterraform\b|\baws\b", re.I),
    re.compile(r"\.env\b|\bcredential|\bsecret|\btoken\b|\bapi[_-]?key", re.I),
    re.compile(r"\bdelete\b|\bdestroy\b|\bwipe\b|\boverwrite\b", re.I),
]

# Shell prompt at the end of the screen: the task really has finished, rather
# than the program merely being quiet while it thinks.
_SHELL_PROMPT = re.compile(r"[>$#❯]\s*$")

# A question that ends with the shell's own prompt characters is the shell, not
# a program asking something.
_ANSWERED_COOLDOWN_SECS = 5.0


class MonitorPolicy(str, Enum):
    """How much the monitor may answer on the user's behalf."""

    ASK = "ask"  # answer only clearly-safe prompts, escalate the rest
    AUTO = "auto"  # answer anything that is not on the danger list
    ALWAYS_YES = "always_yes"  # answer every prompt, no danger check


@dataclass
class MonitorState:
    running: bool = False
    policy: MonitorPolicy = MonitorPolicy.ASK
    answered: int = 0
    checks: int = 0
    awaiting_user: bool = False


def find_prompt(screen: str) -> str:
    """Return the question if the terminal is waiting on one, else "".

    Two things make this stricter than "does the screen contain a question".
    A shell prompt at the end means nothing is waiting for input — the question
    on screen was already answered. And the question has to be in the last few
    lines: text scrolled further up is history, not a live prompt. Without both
    checks the monitor answers a prompt twice, and the second answer lands in
    whatever now has the keyboard.
    """
    stripped = screen.rstrip()
    if not stripped or is_finished(stripped):
        return ""
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    tail = "\n".join(lines[-3:])
    for pattern in _PROMPT_PATTERNS:
        if pattern.search(tail):
            return "\n".join(lines[-6:])
    return ""


def looks_dangerous(text: str) -> str:
    """Return the matched danger phrase, or "" when nothing matches."""
    for pattern in _DANGER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def is_finished(screen: str) -> bool:
    """Whether the pane is back at a shell prompt with nothing running."""
    stripped = screen.rstrip()
    return bool(stripped) and bool(_SHELL_PROMPT.search(stripped.splitlines()[-1]))


class TerminalMonitor:
    """Polls the terminal and reacts, under a policy, until told to stop."""

    def __init__(self, router, task, context):
        self._router = router
        self._task = task
        self._context = context
        self._loop_task: asyncio.Task | None = None
        self.state = MonitorState()
        # Prompt line → when it was answered, so a question that lingers on
        # screen after being answered is not answered again.
        self._recently_answered: dict[str, float] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(
        self,
        policy: MonitorPolicy = MonitorPolicy.ASK,
        watch_for: str = "",
        interval_secs: float = 2.0,
        max_answers: int = 10,
        max_minutes: float = 30.0,
        stop_when_done: bool = True,
    ) -> str:
        if self.state.running:
            return "A monitor is already running. Stop it before starting another."
        self.state = MonitorState(running=True, policy=policy)
        self._loop_task = asyncio.ensure_future(
            self._run(watch_for, interval_secs, max_answers, max_minutes, stop_when_done)
        )
        logger.info(
            f"[MONITOR] started policy={policy.value} interval={interval_secs}s "
            f"watch_for={watch_for!r} max_answers={max_answers} max_minutes={max_minutes} "
            f"stop_when_done={stop_when_done}"
        )
        mode = "until the work finishes" if stop_when_done else "until you tell me to stop"
        return f"Watching the terminal {mode} (policy: {policy.value})."

    def stop(self, reason: str = "asked to stop") -> str:
        if not self.state.running:
            return "No monitor is running."
        self.state.running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        logger.info(f"[MONITOR] stopped ({reason}) after {self.state.checks} checks")
        return f"Stopped monitoring the terminal ({reason})."

    # ── internals ────────────────────────────────────────────────────────────

    async def _speak(self, message: str) -> None:
        """Wake the LLM with an observation so it can respond to the user."""
        self._context.add_message({"role": "user", "content": message})
        await self._task.queue_frames([LLMRunFrame()])

    def _decide(self, prompt: str) -> tuple[bool, str]:
        """Whether to answer this prompt, and why."""
        if self.state.policy is MonitorPolicy.ALWAYS_YES:
            return True, "blanket yes policy"
        danger = looks_dangerous(prompt)
        if danger:
            return False, f"looks dangerous ({danger!r})"
        if self.state.policy is MonitorPolicy.AUTO:
            return True, "no danger match"
        # ASK: only answer a prompt that is unambiguously a yes/no confirmation.
        if re.search(r"\(y(es)?/n(o)?\)|\[y/n\]|❯\s*1\.\s*yes", prompt, re.I):
            return True, "clearly a yes/no confirmation"
        return False, "not clearly safe"

    async def _answer(self, prompt: str, answer: str = "yes") -> bool:
        """Send an answer, but only if that same prompt is still waiting.

        Between deciding and typing, the screen can move on. Answering then
        would apply the decision to whatever now holds the keyboard — at a shell
        prompt, "yes" is a command that runs. So re-read the screen and require
        a live prompt whose last line still matches, and refuse to answer the
        same question twice in quick succession.
        """
        last_line = prompt.splitlines()[-1] if prompt else ""
        if last_line in self._recently_answered:
            logger.warning("[MONITOR] already answered this prompt; not sending again")
            return False

        current = find_prompt(self._router.capture_output())
        if not current:
            logger.warning("[MONITOR] nothing is waiting for input now; not sending")
            return False
        if current.splitlines()[-1] != last_line:
            logger.warning("[MONITOR] the question changed before answering; not sending")
            return False

        await self._router.send_input(answer)
        self._recently_answered[last_line] = asyncio.get_event_loop().time()
        self.state.answered += 1
        logger.warning(f"[MONITOR] auto-answered {answer!r} to: {prompt.splitlines()[-1][:100]}")
        return True

    async def _run(
        self,
        watch_for: str,
        interval_secs: float,
        max_answers: int,
        max_minutes: float,
        stop_when_done: bool = True,
    ) -> None:
        deadline = asyncio.get_event_loop().time() + max_minutes * 60
        matcher = re.compile(watch_for, re.I) if watch_for else None
        last_prompt = ""
        # "Finished" only means something after something started. Monitoring is
        # often requested while the terminal is already sitting at a prompt, and
        # without this the first check would report completion and stop.
        # Completion means "the screen moved on and is now back at a prompt",
        # measured against the first settled poll rather than guessed per frame.
        # The first poll is skipped so a half-drawn screen is not mistaken for a
        # running command — which otherwise makes the next poll look like the
        # work finished the instant monitoring starts.
        settled: str | None = None
        try:
            while self.state.running:
                await asyncio.sleep(interval_secs)
                self.state.checks += 1
                screen = self._router.capture_output()
                now = asyncio.get_event_loop().time()
                self._recently_answered = {
                    line: at
                    for line, at in self._recently_answered.items()
                    if now - at < _ANSWERED_COOLDOWN_SECS
                }

                if matcher and matcher.search(screen):
                    await self._speak(
                        f"[TERMINAL MONITOR] The pattern you asked me to watch for "
                        f"({watch_for!r}) appeared. Screen:\n\n{screen[-1500:]}"
                    )
                    self.stop("pattern matched")
                    return

                prompt = find_prompt(screen)
                if prompt and prompt != last_prompt and not self.state.awaiting_user:
                    last_prompt = prompt
                    answer_it, why = self._decide(prompt)
                    if answer_it and self.state.answered < max_answers:
                        if await self._answer(prompt):
                            continue
                    # Escalate: stop acting and hand the decision to the user.
                    self.state.awaiting_user = True
                    limit_hit = answer_it and self.state.answered >= max_answers
                    reason = f"reached the {max_answers}-answer limit" if limit_hit else why
                    await self._speak(
                        "[TERMINAL MONITOR] The terminal is asking something I should not "
                        f"answer for you — {reason}. Tell the user what it is asking, in one "
                        "or two sentences, and ask how they want to respond. Prompt:\n\n"
                        f"{prompt}"
                    )
                    continue

                if not prompt and self.state.awaiting_user:
                    # User dealt with it; resume watching.
                    self.state.awaiting_user = False

                if settled is None:
                    settled = screen
                    continue

                if not prompt and screen != settled and is_finished(screen):
                    await self._speak(
                        "[TERMINAL MONITOR] The command has finished and the terminal is back "
                        "at a prompt. Summarise the outcome for the user in one or two "
                        f"sentences. Screen:\n\n{screen[-1500:]}"
                    )
                    if stop_when_done:
                        self.stop("work finished")
                        return
                    # Persistent mode: this becomes the new resting state, so the
                    # next command is reported when it in turn finishes.
                    settled = screen

                if asyncio.get_event_loop().time() > deadline:
                    await self._speak(
                        f"[TERMINAL MONITOR] I have been watching for {max_minutes:.0f} minutes "
                        "and am stopping now. Tell the user briefly, and mention they can ask "
                        "me to watch again."
                    )
                    self.stop("time limit")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[MONITOR] loop failed: {e}")
            self.state.running = False
