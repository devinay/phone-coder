"""Shell command tools for the voice coding cockpit."""

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from terminal_monitor import MonitorPolicy, TerminalMonitor


def create_shell_tools(router, task=None, context=None):
    """Create shell command tool functions with access to the router.

    ``task`` and ``context`` are needed only by the background monitor, which
    speaks between turns; without them the monitor tools are not offered.
    """
    monitor = (
        TerminalMonitor(router, task, context) if task is not None and context is not None else None
    )

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
        policy: str = "ask",
        watch_for: str = "",
        interval_secs: float = 2.0,
        max_answers: int = 10,
        max_minutes: float = 30.0,
        stop_when_done: bool = True,
    ):
        """Watch the terminal in the background and react without being asked again.

        Use this when the user wants ongoing attention — "keep an eye on it",
        "let me know when it finishes", "answer yes unless it looks dangerous".
        It stays quiet while work is in progress and speaks only when a prompt
        needs a decision or the work has finished.

        Args:
            policy: How much to answer on the user's behalf.
                "ask" (default) answers only unambiguous yes/no confirmations
                and escalates anything else. "auto" answers anything that is
                not recognisably dangerous. "always_yes" answers every prompt
                with no danger check — only use this if the user has clearly
                asked for it in those terms.
            watch_for: Optional regular expression; report and stop as soon as
                it appears on screen.
            interval_secs: Seconds between checks (default 2.0).
            max_answers: Stop answering after this many prompts (default 10).
            max_minutes: Give up watching after this long (default 30).
            stop_when_done: True (default) stops once the current command
                finishes. Set False when the user wants continuous attention —
                "keep watching", "stay on it" — so it reports each command as it
                completes and keeps polling until told to stop.
        """
        if monitor is None:
            await params.result_callback("Background monitoring is not available.")
            return
        try:
            chosen = MonitorPolicy(policy)
        except ValueError:
            await params.result_callback(
                f"Unknown policy {policy!r}. Use 'ask', 'auto', or 'always_yes'."
            )
            return
        result = monitor.start(
            policy=chosen,
            watch_for=watch_for,
            interval_secs=interval_secs,
            max_answers=max_answers,
            max_minutes=max_minutes,
            stop_when_done=stop_when_done,
        )
        await params.result_callback(result)

    async def stop_terminal_monitor(params: FunctionCallParams):
        """Stop watching the terminal in the background."""
        if monitor is None:
            await params.result_callback("Background monitoring is not available.")
            return
        await params.result_callback(monitor.stop())

    tools = {
        "run_command": run_command,
        "send_input": send_input,
        "capture_output": capture_output,
        "wait_for_output_idle": wait_for_output_idle,
        "watch_terminal": watch_terminal,
        "find_directory": find_directory,
    }
    if monitor is not None:
        tools["start_terminal_monitor"] = start_terminal_monitor
        tools["stop_terminal_monitor"] = stop_terminal_monitor
    return tools
