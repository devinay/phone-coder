"""Shell command tools for the voice coding cockpit."""

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from terminal_monitor import TerminalMonitor


def create_shell_tools(router, task=None, context=None, runtime=None, announce=None):
    """Create shell command tool functions with access to the router.

    ``task`` and ``context`` are needed only by the background monitor, which
    speaks between turns; without them the monitor tools are not offered.
    ``runtime`` lets a wake that needs a keypress arrive as an agent that has
    one, and ``announce`` makes such a wake perceptible when nothing is spoken.
    Both are optional; the monitor degrades to its prompt-only behaviour.
    """
    monitor = (
        TerminalMonitor(router, task, context, runtime=runtime, announce=announce)
        if task is not None and context is not None
        else None
    )
    # Published so teardown can stop it. Its loops are asyncio tasks that poll
    # tmux, and without this they outlived the session that started them —
    # still polling a pane that had just been killed.
    router.monitor = monitor

    async def run_command(params: FunctionCallParams, command: str, directory_path: str = ""):
        """Run a shell command in the terminal. Use this for everything: starting assistants
        (e.g. 'claude', 'codex'), running build tools, git commands, etc.

        Args:
            command: The verbatim shell command to run (e.g. 'claude', 'ls -la', 'git status')
            directory_path: Absolute path to cd into before running. Omit for commands that
                            should run in the current working directory (e.g. pwd, ls, git status,
                            cloud, claude, or any REPL/interactive tool).
        """
        result = await router.run_command(command, directory_path)
        logger.info(f"[TOOL] RUN COMMAND: {command}\n{result}")
        await params.result_callback(result)

    async def send_input(params: FunctionCallParams, text: str):
        """Send text input to whatever is currently running in the terminal.
        Use this to interact with an interactive program (e.g. sending a prompt to claude).

        Args:
            text: The text to send
        """
        result = await router.send_input(text)
        logger.info(f"[TOOL] SEND INPUT: {text}\n{result}")
        await params.result_callback(result)

    async def capture_output(params: FunctionCallParams, lines: int = 50):
        """Capture terminal output as it is right now, without waiting.

        Returns a snapshot, which for a still-running command will be partial.
        Use wait_for_output_idle first when the command needs to finish.

        Args:
            lines: Lines of scrollback to include above the visible screen
                (default 50). Ignored while a full-screen program such as Claude
                Code is running, because those keep no scrollback.
        """
        result = router.capture_output(lines)
        logger.info(f"[TOOL] CAPTURE OUTPUT\n{result}")
        await params.result_callback(result)

    async def terminal_since_last_look(params: FunctionCallParams):
        """Report what has changed in the terminal since you last looked.

        Cheaper and clearer than re-reading the whole screen: it answers
        "nothing has changed" directly, and otherwise returns only the new
        output. Use it to follow a long-running program without re-summarising
        what you already told the user.
        """
        if router.history is None:
            result = "Terminal recording is unavailable; use capture_output instead."
        else:
            changed_text, changed = router.history.since_last_look("agent")
            if not changed:
                result = "Nothing has changed in the terminal since your last look."
            else:
                result = f"New terminal output:\n{changed_text}"
        logger.info(f"[TOOL] TERMINAL SINCE LAST LOOK\n{result}")
        await params.result_callback(result)

    async def send_keys(params: FunctionCallParams, keys: str, pane: str = ""):
        """Press keys in the terminal without sending a line of text.

        Interactive programs are driven by keypresses, not lines. Claude Code's
        permission dialog is a numbered menu: press "1" to accept, or "Enter"
        to take the highlighted option. Sending the word "yes" as text does
        nothing there — use this instead.

        Args:
            keys: Space-separated tmux key names, e.g. "1", "Enter", "Escape",
                "Down Enter", "C-c".
            pane: Which pane to press them in, as reported by list_terminal_panes.
                Leave empty for the main shell pane.
        """
        parts = keys.split()
        if not parts:
            await params.result_callback("No keys given.")
            return
        if pane and not router.pane_exists(pane):
            available = ", ".join(p["target"] for p in router.list_panes()) or "none"
            await params.result_callback(
                f"There is no pane {pane!r}. Available panes: {available}."
            )
            return
        result = await router.send_key(*parts, target=pane)
        # Counts against the standing instruction's action budget, so "accept
        # everything" cannot loop forever on a program that keeps asking. It is
        # charged to the pane acted on, so answering one pane's prompts does not
        # exhaust an unrelated watch on another.
        router.watches.note_action(router.resolve_target(pane))
        logger.info(f"[TOOL] SEND KEYS: {parts} pane={pane or 'default'}\n{result}")
        await params.result_callback(result)

    async def list_terminal_panes(params: FunctionCallParams):
        """List the terminal panes available to watch or act on.

        Use this before watching a second thing — a build in one pane while
        Claude Code runs in another — so you can name the pane you mean.
        """
        panes = router.list_panes()
        if not panes:
            result = "No terminal panes found."
        else:
            result = "\n".join(
                f"{p['target']} — running {p['command'] or 'a shell'} in {p['cwd']}"
                for p in panes
            )
        logger.info(f"[TOOL] LIST PANES\n{result}")
        await params.result_callback(result)

    async def wait_for_output_idle(
        params: FunctionCallParams, idle_secs: float = 2.0, timeout: float = 120.0
    ):
        """Wait until the terminal stops producing output, then return the screen.

        Use this after starting anything slow — builds, tests, Claude Code — so
        that what you summarise is the finished output rather than a
        half-rendered screen.

        Args:
            idle_secs: How long the screen must stay unchanged to count as done
                (default 2.0).
            timeout: Give up waiting after this many seconds (default 120).
        """
        output, reason = await router.wait_for_idle(idle_secs=idle_secs, timeout=timeout)
        logger.info(f"[TOOL] WAIT FOR IDLE ({reason})\n{output}")
        await params.result_callback(f"[settled: {reason}]\n{output}")

    async def watch_terminal(
        params: FunctionCallParams,
        pattern: str = "",
        idle_secs: float = 2.0,
        timeout: float = 300.0,
    ):
        """Keep watching the terminal until something happens, then report back.

        With a pattern, returns as soon as it appears on screen — useful for
        waiting on a prompt or a specific message. Without one, returns when the
        output settles.

        Args:
            pattern: Regular expression to wait for, e.g. "Do you want to" or
                "error". The command line you just ran is itself on screen, so
                choose text that appears in the output rather than in the
                command. Leave empty to wait for the output to settle instead.
            idle_secs: Settle time when no pattern is given (default 2.0).
            timeout: Give up after this many seconds (default 300).
        """
        output, reason = await router.watch(
            pattern=pattern, idle_secs=idle_secs, timeout=timeout
        )
        logger.info(f"[TOOL] WATCH TERMINAL pattern={pattern!r} ({reason})\n{output}")
        await params.result_callback(f"[{reason}]\n{output}")

    async def find_directory(params: FunctionCallParams, directory_name: str):
        """Search for a directory by name up to 3 levels deep from the home directory.
        Use this when the user gives a partial name or relative path.

        Args:
            directory_name: The name or partial path of the directory to find.
        """
        path, is_exact = router.find_best_directory(directory_name)
        if not path:
            result = f"Directory '{directory_name}' not found within 3 levels."
        elif isinstance(path, list):
            result = f"Found multiple matches: {', '.join(path)}. Which one did you mean?"
        else:
            result = f"Found {'exact ' if is_exact else ''}match at '{path}'."

        print(f"\n[TOOL] FIND DIRECTORY: {directory_name}\n{result}\n")
        await params.result_callback(result)

    async def start_terminal_monitor(
        params: FunctionCallParams,
        instruction: str,
        watch_for: str = "",
        pane: str = "",
        interval_secs: float = 2.0,
        max_actions: int = 20,
        max_minutes: float = 30.0,
    ):
        """Keep a standing instruction about the terminal in force.

        Use this when the user wants ongoing attention — "keep an eye on it",
        "let me know when it finishes", "accept defaults but pick the
        always-allow option when it's offered".

        The instruction is not executed by a background process. It is recorded
        and shown to you at the start of every turn, together with whatever the
        terminal is currently asking, and *you* act on it — so an instruction can
        be as specific or conditional as the user likes. A background watcher
        wakes you if something starts waiting while the user has gone quiet.

        Several instructions can be in force at once, on the same pane or on
        different ones — "watch claude, and also tell me when the build breaks"
        is two calls, not one replacing the other.

        Args:
            instruction: The user's instruction, in their own words, as close to
                verbatim as possible. Do not translate it into a policy name or
                simplify a conditional away — "yes, but pick the second option if
                there is one" must be recorded as said, because you will be the
                one applying it.
            watch_for: Optional regular expression; report as soon as it appears.
                Only this instruction retires when it matches; anything else
                being watched carries on.
            pane: Which pane this instruction is about, as reported by
                list_terminal_panes. Leave empty for the main shell pane.
            interval_secs: Seconds between background checks (default 2.0).
            max_actions: Stop acting after this many actions (default 20).
            max_minutes: Drop the instruction after this long (default 30).
        """
        if not instruction.strip():
            await params.result_callback(
                "An instruction is required — record what the user actually asked for."
            )
            return
        # Stored resolved, so the registry, the monitor loop and the action
        # budget all key on the same string whatever the caller typed.
        target = router.resolve_target(pane)
        # A pane name is just a string, so an invented one ("main") would be
        # accepted here and then fail on every poll for the life of the process.
        # Checked once, up front, with the real names offered back.
        if not router.pane_exists(target):
            available = ", ".join(p["target"] for p in router.list_panes()) or "none"
            await params.result_callback(
                f"There is no pane {pane!r}. Available panes: {available}. "
                "Leave `pane` empty for the main shell pane."
            )
            return
        task = router.watches.add(
            instruction=instruction,
            watch_for=watch_for,
            target=target,
            max_minutes=max_minutes,
            max_actions=max_actions,
        )
        started = ""
        if monitor is not None:
            started = monitor.start(
                interval_secs=interval_secs, max_minutes=max_minutes, target=target
            )
        result = (
            f"Standing instruction #{task.id} recorded: {task.instruction!r}. It will be "
            "applied at the start of each turn, and you will be woken if something starts "
            f"waiting while the user is quiet. {started}"
        ).strip()
        logger.info(f"[TOOL] START WATCH #{task.id}: {task.instruction!r} target={target}")
        await params.result_callback(result)

    async def stop_terminal_monitor(params: FunctionCallParams, pane: str = ""):
        """Stop watching and drop standing instructions.

        Args:
            pane: Stop watching only this pane. Leave empty to stop everything.
        """
        if pane:
            target = router.resolve_target(pane)
            dropped = router.watches.clear(target)
            stopped = monitor.stop("asked to stop", target) if monitor is not None else ""
        else:
            dropped = router.watches.clear()
            stopped = monitor.stop() if monitor is not None else ""
        result = f"Dropped {dropped} standing instruction(s). {stopped}".strip()
        logger.info(f"[TOOL] STOP WATCH: {result}")
        await params.result_callback(result)

    tools = {
        "run_command": run_command,
        "send_input": send_input,
        "send_keys": send_keys,
        "capture_output": capture_output,
        "terminal_since_last_look": terminal_since_last_look,
        "wait_for_output_idle": wait_for_output_idle,
        "watch_terminal": watch_terminal,
        "find_directory": find_directory,
        "list_terminal_panes": list_terminal_panes,
    }
    if monitor is not None:
        tools["start_terminal_monitor"] = start_terminal_monitor
        tools["stop_terminal_monitor"] = stop_terminal_monitor
    return tools
