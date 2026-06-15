"""Shell command tools for the voice coding cockpit."""

from pipecat.services.llm_service import FunctionCallParams
from loguru import logger


def create_shell_tools(router):
    """Create shell command tool functions with access to the router."""

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
        """Capture recent terminal output to check what happened.

        Args:
            lines: Number of lines to capture (default 50)
        """
        result = router.capture_output(lines)
        logger.info(f"[TOOL] CAPTURE OUTPUT\n{result}")
        await params.result_callback(result)

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

    return {
        "run_command": run_command,
        "send_input": send_input,
        "capture_output": capture_output,
        "find_directory": find_directory,
    }
