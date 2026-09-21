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

Two things it does do around a wake, because neither can be left to a prompt:

* **It wakes the model as an agent with hands.** A wake lands on whatever agent
  is active, normally the controller, which can read the terminal but not type
  into it. When the wake is about something waiting for input, the runtime is
  switched to an agent that has ``send_keys`` first.
* **It announces.** A monitor report is the one message the user never asked
  for, so it is the one that can go unnoticed — text-only mode marks replies
  ``skip_tts`` and a quiet spell mutes the mic. The wake also goes out on its
  own channel, which does not depend on either.
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

# A multi-select list, which is driven completely differently from the numbered
# radio list beside it and was previously indistinguishable from one. Claude
# Code renders its options as "1. [ ] Caching" / "2. [x] Minify".
#
# Measured against a real AskUserQuestion multiSelect dialog (see
# tests/fixtures/claude-code-multiselect.txt). The distinction matters because
# the keys mean different things: in a radio list a number *chooses and
# advances*, while here it only *toggles* and the dialog stays open. An agent
# that does not know the difference presses 1, sees nothing happen, and gets
# stuck — which is exactly what happened.
_CHECKBOX_OPTION = re.compile(r"^\s*\d+\.\s*\[[ x✔✓X]?\]", re.M)

# A bare question sitting on the very last line, which is the classic shell
# confirm shape: `rm -i` asks "remove /tmp/x?" and nothing else — no "(y/n)", no
# "do you want to", nothing the patterns above look for. Anchored to the last
# line only, so a question inside a paragraph of output does not trip it.
_TRAILING_QUESTION = re.compile(r"\?\s*$")

# Shell prompt at the end of the screen: the task really has finished, rather
# than the program merely being quiet while it thinks.
_SHELL_PROMPT = re.compile(r"[>$#❯]\s*$")

# How many trailing non-blank lines count as "live". A TUI dialog puts its
# question several lines above the last option, so 3 lines (the original window)
# could never see it. It stays deliberately small: widen this far enough and a
# question that already scrolled past looks live again.
#
# Measured, not guessed. Two real Claude Code dialogs captured from a 100x30
# tmux pane put the question this far from the bottom, counting non-blank lines:
#
#   Bash permission ("Do you want to proceed?", 3 options)      5
#   Edit permission ("Do you want to make this edit to X?",     6
#     3 options, one wrapping onto a second line)
#
# So 15 clears the worst observed case by 9 lines, which is the margin a longer
# option list or more wrapping would eat into.
_PROMPT_WINDOW = 15

# How much to hand back once a prompt *is* found. Detection has to be narrow;
# deciding does not, and the two were conflated. The real edit dialog fills the
# whole screen — the diff being approved sits above the question — so returning
# only the detection window asked the model to approve a change it could not
# see. Wide enough for a screenful, still bounded.
_PROMPT_CONTEXT = 40

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
    """Per-pane loop state. One of these exists for each watched target."""

    running: bool = False
    checks: int = 0
    wakes: int = 0
    awaiting_user: bool = False
    # Normalised screen the model was last woken about, so a prompt that simply
    # stays on screen does not wake it again every tick.
    last_woken_for: str = ""
    # "Finished" only means something after something started. Monitoring is
    # often requested while the pane already sits at a prompt, so the first
    # settled poll becomes the baseline rather than a completion.
    settled: str | None = None


# Reasons that need a key pressed, so the model has to wake as an agent that
# can press one. FINISHED and EXPIRED are reports; the controller can give those.
_NEEDS_HANDS = {WakeReason.PROMPT, WakeReason.PATTERN}


def find_prompt(screen: str, alternate_screen: bool = False) -> str:
    """Return the question if the terminal is waiting on one, else "".

    Stricter than "does the screen contain a question". A shell prompt at the
    end means nothing is waiting — the question on screen was already answered.
    And the question has to be near the bottom: text scrolled further up is
    history, not a live prompt.

    Box-drawing characters are stripped before matching, because a full-screen
    program draws its question inside a border and the border otherwise hides
    the text from every pattern.

    Detection looks at the last ``_PROMPT_WINDOW`` lines; what comes back is the
    last ``_PROMPT_CONTEXT`` lines, so the model sees the diff or command it is
    being asked to approve and not just the question about it.
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
    if any(pattern.search(tail) for pattern in _PROMPT_PATTERNS) or _TRAILING_QUESTION.search(
        lines[-1]
    ):
        return "\n".join(lines[-_PROMPT_CONTEXT:])
    return ""


def is_multi_select(screen: str) -> bool:
    """Whether the dialog on screen is a multi-select rather than a radio list.

    Both are numbered lists and they look almost identical, but the keys do
    different things, so telling them apart is the whole point.
    """
    return bool(_CHECKBOX_OPTION.search(strip_borders(screen)))


def how_to_answer(screen: str) -> str:
    """The key sequence this particular dialog needs, or "" if it is ordinary.

    Only multi-select earns an explanation: a radio list does what everyone
    already expects, whereas here pressing the number of the option you want
    does *not* answer the question, and nothing on screen says so clearly.

    Verified by driving a real AskUserQuestion multiSelect dialog: the numbers
    toggle without moving the cursor, Enter toggles the highlighted row, Right
    moves to the Submit tab, and the review screen that appears there is an
    ordinary radio list where 1 is "Submit answers".
    """
    if not is_multi_select(screen):
        return ""
    return (
        "This is a MULTI-SELECT list, not a normal menu. Pressing a number only "
        "ticks that option on or off — it does not answer the question, and the "
        "dialog will not move on. To answer it: send_keys with the number of "
        "every option you want (each toggles independently), then send_keys "
        "\"Right\" to move to the Submit tab, then send_keys \"1\" to confirm on "
        "the review screen. Check the boxes read [✔] before submitting."
    )


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
    """Watches panes and wakes the LLM. Decides nothing, presses nothing.

    One loop per watched pane, keyed by tmux target, so "watch claude and also
    watch the build" is two loops rather than the second request being refused.
    A loop lives exactly as long as the pane has instructions in the registry.
    """

    def __init__(self, router, task, context, runtime=None, announce=None):
        self._router = router
        self._task = task
        self._context = context
        # Optional. With it, a wake that needs a keypress arrives as an agent
        # that can press one; without it, that hop stays prompt-only.
        self._runtime = runtime
        # Optional. Called with a one-line summary so a wake is visible even
        # when speech is off or the mic has been idle-muted.
        self._announce = announce
        self._loops: dict[str, asyncio.Task] = {}
        self._states: dict[str, MonitorState] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def state(self) -> MonitorState:
        """The default pane's state. Kept for callers that watch one pane."""
        return self._states.get(self._default_target(), MonitorState())

    @property
    def watched(self) -> list[str]:
        return [t for t, loop in self._loops.items() if not loop.done()]

    def _default_target(self) -> str:
        resolve = getattr(self._router, "default_target", None)
        return resolve() if resolve else ""

    def start(
        self,
        interval_secs: float = 2.0,
        max_minutes: float = 30.0,
        target: str = "",
    ) -> str:
        """Begin watching one pane. The instruction lives in the watch registry.

        Starting a watch on a pane that is already watched is not an error and
        not a no-op either: the new instruction is already in the registry, and
        the running loop picks it up on its next tick.
        """
        target = target or self._default_target()
        where = f"pane {target}" if target else "the terminal"
        existing = self._loops.get(target)
        if existing and not existing.done():
            return f"Already watching {where}; the new instruction joins it."
        self._states[target] = MonitorState(running=True)
        self._loops[target] = asyncio.ensure_future(
            self._run(target, interval_secs, max_minutes)
        )
        logger.info(
            f"[MONITOR] started target={target or 'default'} interval={interval_secs}s "
            f"max_minutes={max_minutes} (trigger-only: wakes the LLM, never sends keys)"
        )
        return f"Watching {where}."

    def stop(self, reason: str = "asked to stop", target: str | None = None) -> str:
        """Stop one pane's loop, or every loop when no target is given."""
        targets = list(self._loops) if target is None else [target or self._default_target()]
        stopped = []
        for name in targets:
            state = self._states.get(name)
            loop = self._loops.pop(name, None)
            if state is None or not state.running:
                continue
            state.running = False
            if loop:
                loop.cancel()
            stopped.append(name)
            logger.info(
                f"[MONITOR] stopped target={name or 'default'} ({reason}) after "
                f"{state.checks} checks, {state.wakes} wake(s)"
            )
        if not stopped:
            return "Not watching the terminal."
        if len(stopped) == 1:
            where = f"pane {stopped[0]}" if stopped[0] else "the terminal"
            return f"Stopped watching {where} ({reason})."
        return f"Stopped watching {len(stopped)} panes ({reason})."

    # ── internals ────────────────────────────────────────────────────────────

    async def _escalate(self, reason: WakeReason) -> None:
        """Wake as an agent that can act, not merely one that can look.

        The monitor fires between turns, so the model wakes as whatever agent is
        active — normally the controller, which can read the terminal but cannot
        press a key. Leaving that hop to the prompt meant a wake that needed one
        keystroke could end in a description of the keystroke instead.
        """
        if self._runtime is None or reason not in _NEEDS_HANDS:
            return
        try:
            if "send_keys" in self._runtime.active_spec.tool_names:
                return
            agent_id = self._runtime.agent_for_tool("send_keys")
            if not agent_id:
                logger.warning("[MONITOR] no agent provides send_keys; staying put")
                return
            previous = self._runtime.active_agent_id
            self._runtime.apply_agent(
                agent_id,
                user_request="[TERMINAL MONITOR] the terminal is waiting for input",
                preserve_context=True,
                source_agent=previous,
            )
            # Each agent declares its own model; switching agent without it
            # would run the shell prompt on the controller's model.
            await self._runtime.apply_agent_model(agent_id, preserve_context=True)
            logger.info(f"[MONITOR] escalated {previous} → {agent_id} to answer the terminal")
        except Exception as e:
            # An escalation failure must not swallow the wake itself.
            logger.error(f"[MONITOR] could not escalate: {e}")

    async def _notify(self, reason: WakeReason, target: str) -> None:
        """Make the wake perceptible even when nothing will be spoken.

        A monitor report is the one thing the user did not ask for and is not
        waiting on a reply to, so it is exactly the thing that must not arrive
        silently — and it can: text-only mode marks replies skip_tts, and a
        quiet spell mutes the mic. This goes out over its own channel.
        """
        if self._announce is None:
            return
        where = f" (pane {target})" if target else ""
        try:
            await self._announce(reason.value, f"Terminal: {reason.value}{where}")
        except Exception as e:
            logger.warning(f"[MONITOR] could not announce: {e}")

    async def _wake(self, reason: WakeReason, screen: str, target: str = "") -> None:
        """Hand the situation to the LLM, with no opinion about what to do.

        The standing instruction is already in the system prompt via the watch
        registry, so this deliberately does not restate or interpret it.
        """
        state = self._states.setdefault(target, MonitorState(running=True))
        state.wakes += 1
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
        where = f" on pane {target}" if target else ""
        keys = how_to_answer(screen)
        if keys:
            headline += "\n\n[HOW TO ANSWER IT] " + keys
        await self._escalate(reason)
        self._context.add_message(
            {
                "role": "user",
                "content": (
                    f"[TERMINAL MONITOR{where}] {headline}\n\nScreen:\n\n{screen[-4000:]}"
                ),
            }
        )
        await self._notify(reason, target)
        await self._task.queue_frames([LLMRunFrame()])
        logger.info(f"[MONITOR] woke the model ({reason.value}) target={target or 'default'}")

    def _user_is_quiet(self) -> bool:
        """Whether the user has been silent long enough to interrupt.

        While they are talking there is no need: the next turn boundary services
        the terminal anyway.
        """
        idle = getattr(self._router, "seconds_since_user_spoke", None)
        if idle is None:
            return True
        return idle() >= _QUIET_BEFORE_WAKE_SECS

    async def _run(self, target: str, interval_secs: float, max_minutes: float) -> None:
        deadline = asyncio.get_event_loop().time() + max_minutes * 60
        state = self._states[target]
        try:
            while state.running:
                await asyncio.sleep(interval_secs)
                state.checks += 1

                watches = getattr(self._router, "watches", None)
                mine = watches.for_target(target) if watches else []
                if watches is not None and not mine:
                    # The last instruction for this pane was answered, expired or
                    # cleared. The loop exists to serve them, so it goes too —
                    # this is what keeps a per-pane loop from outliving its pane.
                    self.stop("no instructions left", target)
                    return

                # The pane can go away underneath a watch — closed by the user,
                # or never there at all because the target was invented. Either
                # way every call against it fails, so the loop stops instead of
                # logging a tmux error every tick for the life of the process.
                exists = getattr(self._router, "pane_exists", None)
                if exists is not None and not exists(target):
                    logger.warning(
                        f"[MONITOR] pane {target or 'default'} is gone; dropping its watches"
                    )
                    if watches is not None:
                        watches.clear(target)
                    self.stop("pane gone", target)
                    return

                screen = self._router.capture_output(target=target)
                alt = self._router.on_alternate_screen(target=target)

                # A pattern the user named takes priority over everything, and
                # retires only its own instruction: matching "build failed" must
                # not also stop the watch that is answering Claude Code.
                matched = next((t for t in mine if t.watch_for and self._matches(t.watch_for, screen)), None)
                if matched is not None:
                    await self._wake(WakeReason.PATTERN, screen, target)
                    if watches is not None:
                        watches.drop(matched, "pattern matched")
                    if watches is not None and not watches.for_target(target):
                        self.stop("pattern matched", target)
                        return
                    continue

                prompt = find_prompt(screen, alt)
                if prompt and self._user_is_quiet():
                    marker = normalise(prompt)
                    if marker != state.last_woken_for:
                        state.last_woken_for = marker
                        await self._wake(WakeReason.PROMPT, screen, target)
                    continue

                if not prompt:
                    # The question went away, so a future identical one is new.
                    state.last_woken_for = ""

                if state.settled is None:
                    state.settled = screen
                    continue

                if (
                    not prompt
                    and not same_screen(screen, state.settled)
                    and is_finished(screen, alt)
                    and self._user_is_quiet()
                ):
                    await self._wake(WakeReason.FINISHED, screen, target)
                    self.stop("work finished", target)
                    return

                if asyncio.get_event_loop().time() > deadline:
                    await self._wake(WakeReason.EXPIRED, screen, target)
                    self.stop("time limit", target)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[MONITOR] loop failed target={target or 'default'}: {e}")
            state.running = False

    @staticmethod
    def _matches(pattern: str, screen: str) -> bool:
        try:
            matcher = re.compile(pattern, re.I)
        except re.error:
            return False
        return bool(matcher.search(screen) or matcher.search(strip_borders(screen)))
