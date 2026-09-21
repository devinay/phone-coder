"""Integration run against a real tmux session, covering the four gaps.

Not a unit test, and deliberately not named ``test_*`` so pytest leaves it
alone: it kills and recreates the ``cockpit`` tmux session, opens a second
window, and runs real programs in both. Everything the unit tests stub — tmux,
ps, the pane's tty — is real here, which is what caught the ``rm -i`` prompt the
pattern list used to miss.

Run from ``server/``:

    uv run python tests/integration_terminal_panes.py

Exits non-zero if any check fails. It tears the session down on the way out, so
do not run it while you are using the cockpit.
"""

import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_router import AgentRouter
from terminal_monitor import TerminalMonitor, WakeReason

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


class Ctx:
    def __init__(self):
        self.messages = []

    def add_message(self, m):
        self.messages.append(m)


class Task:
    def __init__(self):
        self.frames = []

    async def queue_frames(self, f):
        self.frames.extend(f)


class Runtime:
    def __init__(self):
        self.active_agent_id = "controller"
        self.controller_agent_id = "controller"
        self._tools = {"controller": ["capture_output"], "shell": ["send_keys"]}
        self.applied = []

    class Spec:
        def __init__(self, i, t):
            self.agent_id, self.tool_names = i, t

    @property
    def active_spec(self):
        return self.Spec(self.active_agent_id, self._tools[self.active_agent_id])

    def agent_for_tool(self, t):
        if t in self.active_spec.tool_names:
            return self.active_agent_id
        return next((a for a, ts in self._tools.items() if t in ts and a != "controller"), "")

    def apply_agent(self, agent_id, **kw):
        self.applied.append(agent_id)
        self.active_agent_id = agent_id

    async def apply_agent_model(self, agent_id, **kw):
        pass


def refuse_if_a_bot_is_running() -> None:
    """Do not run while a cockpit server is up.

    This script kills and recreates the `cockpit` tmux session. Run against a
    live server it destroys the terminal out from under it, and because the
    session is the only way the agent knows a shell exists, the controller then
    tells the user — correctly — that it has no terminal. That happened; hence
    this guard.
    """
    # Anchored to a python process actually running bot.py. A bare "bot.py"
    # also matches any shell whose command line merely mentions it — including
    # the one that launched this script — which would block a legitimate run.
    found = subprocess.run(
        ["pgrep", "-f", r"python[0-9.]*\s+.*bot\.py"], capture_output=True, text=True
    ).stdout.split()
    if not found:
        return
    if os.environ.get("COCKPIT_INTEGRATION_FORCE") == "1":
        print(f"WARNING: bot.py is running (pid {', '.join(found)}); continuing anyway")
        return
    sys.exit(
        f"Refusing to run: bot.py is running (pid {', '.join(found)}).\n"
        "This script kills the cockpit tmux session, which would break that "
        "server's terminal.\nStop the server first, or set "
        "COCKPIT_INTEGRATION_FORCE=1 if you really mean it."
    )


async def main():
    refuse_if_a_bot_is_running()
    router = AgentRouter()
    router.reset_session()
    await asyncio.sleep(1)

    # ── the pane tap survives a session reset ────────────────────────────────
    # It used to not: start_history() returns early when it has already run, and
    # only runs at startup, so a killed session left the tap pointing at a pane
    # that no longer existed and history silently recorded nothing. Checked here,
    # before the second window exists, because a reset takes every window with it.
    router.start_history()
    first_tap = router.history
    check("history is tapped", first_tap is not None)
    router.reset_session()
    await asyncio.sleep(1)
    check(
        "the tap is rebuilt after a reset",
        router.history is not None and router.history is not first_tap,
    )
    await router.run_command("echo AFTERRESET")
    await asyncio.sleep(1.5)
    check(
        "output after a reset is still recorded",
        "AFTERRESET" in router.capture_output(lines=50),
    )

    # A second pane, so "watch two things" is a real question and not a stub.
    subprocess.run(
        ["tmux", "new-window", "-d", "-t", router.SESSION, "-n", "build", "fish"],
        check=True,
    )
    await asyncio.sleep(1)

    panes = router.list_panes()
    targets = [p["target"] for p in panes]
    check("both panes are listed", len(panes) == 2, ", ".join(targets))


    build = next((t for t in targets if "build" in t), "")
    shell = next((t for t in targets if "shell" in t), "")

    # ── each pane is read independently ──────────────────────────────────────
    await router.run_command("echo SHELLPANE", target=shell)
    await router.run_command("echo BUILDPANE", target=build)
    check("shell pane reads its own screen", "SHELLPANE" in router.capture_output(target=shell))
    check("build pane reads its own screen", "BUILDPANE" in router.capture_output(target=build))
    check(
        "panes do not bleed into each other",
        "BUILDPANE" not in router.capture_output(target=shell),
    )

    # ── gap 1: two watches, two loops ────────────────────────────────────────
    announced = []

    async def announce(reason, text):
        announced.append((reason, text))

    runtime = Runtime()
    task, ctx = Task(), Ctx()
    monitor = TerminalMonitor(router, task, ctx, runtime=runtime, announce=announce)

    router.watches.add("answer claude", target=shell)
    router.watches.add("tell me when it breaks", watch_for="BUILD FAILED", target=build)
    monitor.start(interval_secs=0.5, target=shell)
    monitor.start(interval_secs=0.5, target=build)
    check("two panes watched at once", sorted(monitor.watched) == sorted([build, shell]))

    # ── gap 1: a pattern retires only its own instruction ────────────────────
    await router.run_command("echo BUILD FAILED", target=build)
    await asyncio.sleep(2.0)
    remaining = [t.instruction for t in router.watches.active]
    check(
        "pattern match retires only its own watch",
        remaining == ["answer claude"],
        f"left: {remaining}",
    )
    check("the other pane's loop survives", shell in monitor.watched, str(monitor.watched))
    check(
        "the pattern wake names the pane",
        any(build in m["content"] for m in ctx.messages),
    )

    # ── gap 2 + 3: a real dialog wakes, escalates and announces ──────────────
    runtime.active_agent_id = "controller"
    runtime.applied.clear()
    ctx.messages.clear()
    announced.clear()

    # The file has to exist before the command runs. Removing a missing file
    # errors and drops straight back to the shell prompt, which the monitor
    # correctly reads as FINISHED — and a FINISHED wake stops the loop and does
    # not escalate, so the real prompt never got a chance to arrive.
    subprocess.run(["touch", "/tmp/cockpit-probe.txt"])
    monitor.start(interval_secs=0.5, target=shell)
    await router.run_command("rm -i /tmp/cockpit-probe.txt", target=shell)
    await asyncio.sleep(3.0)

    screen = router.capture_output(target=shell)
    check("a real confirmation prompt is on screen", "remove" in screen.lower(), screen.strip()[-60:])
    check(
        "the model was woken for it",
        any("[TERMINAL MONITOR" in m["content"] for m in ctx.messages),
    )
    check(
        "it woke as an agent that can press a key",
        runtime.applied == ["shell"],
        str(runtime.applied),
    )
    check("the wake was announced out of band", bool(announced), str(announced[:1]))

    # ── the block the model actually reads names the right pane ─────────────
    block = router.terminal_context_block()
    check("[WATCHING] is in the turn block", "[WATCHING]" in block)
    check("[WAITING] is in the turn block", "[WAITING]" in block, block[-120:].replace("\n", " | "))

    # ── the action budget is charged per pane ───────────────────────────────
    router.watches.add("watch the build again", target=build, max_actions=2)
    before = [t.acted for t in router.watches.for_target(build)]
    router.watches.note_action(shell)
    after = [t.acted for t in router.watches.for_target(build)]
    check("acting on one pane does not charge another", before == after, f"{before} -> {after}")

    monitor.stop()
    await router.send_key("n", "Enter", target=shell)
    router.cleanup()
    subprocess.run(["rm", "-f", "/tmp/cockpit-probe.txt"])

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
