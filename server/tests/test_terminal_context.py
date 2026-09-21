"""Tests for terminal awareness: vocabulary, screen normalisation, foreground
state, prompt detection and rendered history.

Each group pins down a bug that made "watch Claude and answer yes" fail.
"""

import asyncio
from pathlib import Path

import pytest

from terminal_history import TerminalHistory
from terminal_monitor import (
    _PROMPT_WINDOW,
    TerminalMonitor,
    WakeReason,
    find_prompt,
    how_to_answer,
    is_finished,
    is_multi_select,
)
from terminal_screen import normalise, same_screen, strip_borders
from terminal_state import (
    Observation,
    current_shell,
    format_age,
    pane_owner,
    parse_etime,
    parse_ps,
    program_name,
)
from terminal_tasks import WatchRegistry
from terminal_vocab import normalise_transcript

# A Claude Code permission dialog: bordered, with the question five lines above
# the last option. The original 3-line detection window could never see it.
DIALOG = """Claude Code v2.1
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│                                              │
│   ls -la                                     │
│   List files in the current directory        │
│                                              │
│ Do you want to proceed?                      │
│ ❯ 1. Yes                                     │
│   2. Yes, and don't ask again                │
│   3. No, and tell Claude what to do          │
╰──────────────────────────────────────────────╯
✳ Thinking… (5s · ↑ 1385 tokens)"""

# The real thing. See tests/fixtures/README.md for how these were captured and
# what they settled; the constructed DIALOG above only ever tested that the code
# agrees with itself.
_FIXTURES = Path(__file__).parent / "fixtures"
REAL_BASH_DIALOG = (_FIXTURES / "claude-code-bash-permission.txt").read_text()
REAL_EDIT_DIALOG = (_FIXTURES / "claude-code-edit-permission.txt").read_text()
REAL_MULTISELECT = (_FIXTURES / "claude-code-multiselect.txt").read_text()
REAL_MS_REVIEW = (_FIXTURES / "claude-code-multiselect-review.txt").read_text()


class TestTranscriptRepair:
    @pytest.mark.parametrize(
        "said",
        [
            "launch cloud in that directory",
            "can you start cloud for me",
            "cloud is asking something in the terminal",
            "ask cloud to summarise the file",
            "watch clouds output and answer yes",
            "tell clawed to run the tests",
            "quit cloud",
        ],
    )
    def test_terminal_context_becomes_claude(self, said):
        out, changed = normalise_transcript(said)
        assert changed
        assert "Claude" in out

    @pytest.mark.parametrize(
        "said",
        [
            "deploy this to the cloud",
            "we should run it on a cloud provider",
            "start the aws cloud migration",
            "the cloud storage bucket is full",
            "check the cloud bill",
            "the private cloud region is down",
        ],
    )
    def test_genuine_cloud_survives(self, said):
        """A false positive puts a word in the user's mouth; worse than the bug."""
        out, changed = normalise_transcript(said)
        assert not changed
        assert out == said

    def test_possessive_is_kept(self):
        out, _ = normalise_transcript("watch clouds output")
        assert out == "watch Claude's output"


class TestScreenNormalisation:
    def test_spinner_and_counter_changes_compare_equal(self):
        """The bug that made every wait run to timeout."""
        base = "Building the thing\n"
        frames = [
            base + "✳ Thinking… (3s · ↑ 1.2k tokens)",
            base + "✻ Thinking… (4s · ↑ 1.3k tokens)",
            base + "· Crunching… (11s · ↑ 2.1k tokens)",
            base + "⠋ Working… (12s)",
        ]
        assert all(same_screen(frames[0], f) for f in frames[1:])

    def test_real_change_is_still_seen(self):
        a = "Tests: 3 passed\n✳ Thinking… (3s)"
        b = "Tests: 4 passed\n✳ Thinking… (4s)"
        assert not same_screen(a, b)

    def test_progress_bars_are_masked(self):
        assert same_screen("Build ████░░░ 40%", "Build ██████░ 80%")

    def test_borders_stripped(self):
        assert strip_borders("│ ❯ 1. Yes │").strip() == "❯ 1. Yes"

    def test_normalise_is_stable(self):
        text = "x\n✳ Thinking… (1s · ↑ 5 tokens)"
        assert normalise(text) == normalise(normalise(text))


class TestPromptDetection:
    def test_bordered_dialog_is_found(self):
        """Regression: the 3-line window could not see past the box border."""
        assert find_prompt(DIALOG, alternate_screen=True)

    def test_question_text_survives_the_border(self):
        found = find_prompt(DIALOG, alternate_screen=True)
        assert "Do you want to proceed?" in found
        assert "1. Yes" in found

    def test_nothing_waiting_at_a_shell_prompt(self):
        assert find_prompt("$ ") == ""

    def test_tui_is_never_finished(self):
        """Claude Code's input box ends in '>', which is not a shell prompt."""
        assert not is_finished("bash-3.2$", alternate_screen=True)
        assert is_finished("bash-3.2$", alternate_screen=False)

    def test_scrolled_past_question_is_not_live(self):
        stale = "Do you want to proceed?\n" + "\n".join(f"output {i}" for i in range(30))
        assert find_prompt(stale + "\n$ ") == ""

    def test_a_bare_shell_confirm_is_a_prompt(self):
        """`rm -i` asks "remove /tmp/x?" and nothing the other patterns look for.

        Found by running it against a real pane: the monitor sat there while the
        shell blocked, because every pattern wanted "(y/n)" or "do you want to".
        """
        assert find_prompt("$ rm -i /tmp/x\nremove /tmp/x?")

    def test_a_question_in_passing_output_is_not_a_prompt(self):
        """Anchored to the last line, so prose in a build log does not trip it."""
        assert find_prompt("Did you mean to import this?\nBuilding...\n" + "x\n" * 5) == ""


class TestRealDialogGeometry:
    """The window was sized against a constructed dialog. These are real ones.

    Captured from `claude --model haiku` in a 100x30 tmux pane; see
    tests/fixtures/README.md.
    """

    def _question_depth(self, screen: str) -> int:
        """How far the question sits from the bottom, in non-blank lines.

        The same counting find_prompt does, so this measures the margin the
        detection window actually has rather than an approximation of it.
        """
        lines = [ln.strip() for ln in screen.rstrip().splitlines() if ln.strip()]
        for depth, line in enumerate(reversed(lines), start=1):
            if "do you want to" in line.lower():
                return depth
        raise AssertionError("fixture has no question in it")

    @pytest.mark.parametrize(
        "screen,expected_depth",
        [(REAL_BASH_DIALOG, 5), (REAL_EDIT_DIALOG, 6)],
        ids=["bash-permission", "edit-permission"],
    )
    def test_question_falls_inside_the_window(self, screen, expected_depth):
        assert self._question_depth(screen) == expected_depth
        assert expected_depth < _PROMPT_WINDOW

    @pytest.mark.parametrize(
        "screen", [REAL_BASH_DIALOG, REAL_EDIT_DIALOG], ids=["bash", "edit"]
    )
    def test_real_dialog_is_detected(self, screen):
        assert find_prompt(screen, alternate_screen=True)

    def test_always_allow_option_survives_in_full(self):
        """The abstract instruction case: the model has to read option 2 to pick it."""
        found = find_prompt(REAL_BASH_DIALOG, alternate_screen=True)
        assert "2. Yes, and always allow access to /tmp from this project" in found

    def test_wrapped_option_keeps_its_second_line(self):
        found = find_prompt(REAL_EDIT_DIALOG, alternate_screen=True)
        assert "2. Yes, and switch to accept edits" in found
        assert "session (shift+tab)" in found

    def test_the_diff_being_approved_comes_back_too(self):
        """Detection is narrow; what comes back must be wide enough to decide on.

        The edit dialog fills the screen — the diff is above the question. A
        15-line reply asked the model to approve a change it could not see.
        """
        found = find_prompt(REAL_EDIT_DIALOG, alternate_screen=True)
        assert "+alpha" in found
        assert "-k" in found

    def test_the_command_being_approved_comes_back_too(self):
        found = find_prompt(REAL_BASH_DIALOG, alternate_screen=True)
        assert "touch /tmp/perm-probe.txt" in found


class TestMultiSelectDialogs:
    """A checkbox list is not a menu, and pressing its numbers does not answer it.

    Found in a real session: the agent pressed 1, nothing advanced, it pressed
    the arrows, the highlight went to the wrong row, and the user had to take
    over. Both fixtures come from driving a real AskUserQuestion multiSelect.
    """

    def test_a_multiselect_is_recognised(self):
        assert is_multi_select(REAL_MULTISELECT)

    @pytest.mark.parametrize(
        "screen", [REAL_BASH_DIALOG, REAL_EDIT_DIALOG], ids=["bash", "edit"]
    )
    def test_a_radio_dialog_is_not_mistaken_for_one(self, screen):
        """The costly direction: radio dialogs are the common case."""
        assert not is_multi_select(screen)
        assert how_to_answer(screen) == ""

    def test_a_multiselect_is_still_detected_as_a_live_prompt(self):
        assert find_prompt(REAL_MULTISELECT, alternate_screen=True)

    def test_the_guidance_names_the_keys_that_actually_work(self):
        """Verified by driving the real dialog: numbers toggle, Right, then 1."""
        guidance = how_to_answer(REAL_MULTISELECT)
        assert "MULTI-SELECT" in guidance
        assert "Right" in guidance
        assert "toggle" in guidance.lower()

    def test_a_ticked_box_is_recognised_too(self):
        """Half-answered is the state the agent will usually come back to."""
        assert is_multi_select("1. [✔] Caching\n2. [ ] Minify")
        assert is_multi_select("1. [x] Caching\n2. [ ] Minify")

    def test_the_review_screen_is_an_ordinary_radio_list(self):
        """Right leads here, and this one *does* answer to "1"."""
        assert not is_multi_select(REAL_MS_REVIEW)
        assert "Submit answers" in REAL_MS_REVIEW


class TestWatchRegistry:
    """Standing instructions are kept verbatim and applied by the model.

    The old design regex-matched a prompt and pressed "1". That contradicted any
    instruction more specific than "yes", so deciding moved to turn boundaries
    and this registry is what carries the instruction there.
    """

    def test_instruction_is_kept_verbatim(self):
        said = "accept defaults, but if there's an always-allow option pick that"
        reg = WatchRegistry()
        task = reg.add(said)
        assert task.instruction == said
        assert said in reg.describe()

    def test_empty_registry_injects_nothing(self):
        assert WatchRegistry().describe() == ""
        assert not WatchRegistry()

    def test_multiple_instructions_are_all_listed(self):
        reg = WatchRegistry()
        reg.add("accept defaults")
        reg.add("tell me when the tests finish")
        described = reg.describe()
        assert "accept defaults" in described
        assert "tell me when the tests finish" in described

    def test_action_budget_is_exhaustible(self):
        """"Accept everything" must not loop forever on a program that keeps asking."""
        reg = WatchRegistry()
        reg.add("accept everything", max_actions=2)
        reg.note_action()
        reg.note_action()
        assert reg.prune()
        assert not reg.active

    def test_expired_instruction_is_dropped(self):
        reg = WatchRegistry()
        task = reg.add("watch it", max_minutes=30)
        task.started_at -= 31 * 60
        assert reg.prune()
        assert not reg.active

    def test_clear_drops_everything(self):
        reg = WatchRegistry()
        reg.add("a")
        reg.add("b")
        assert reg.clear() == 2
        assert not reg.active

    def test_instructions_are_kept_per_pane(self):
        reg = WatchRegistry()
        reg.add("answer claude", target="cockpit:shell")
        reg.add("tell me when the build breaks", target="cockpit:build")
        assert reg.targets() == ["cockpit:shell", "cockpit:build"]
        assert len(reg.for_target("cockpit:shell")) == 1

    def test_clearing_one_pane_leaves_the_other(self):
        reg = WatchRegistry()
        reg.add("answer claude", target="cockpit:shell")
        keep = reg.add("watch the build", target="cockpit:build")
        assert reg.clear("cockpit:shell") == 1
        assert reg.active == [keep]

    def test_action_budget_is_charged_to_the_pane_acted_on(self):
        """Answering one pane's prompts must not exhaust another pane's watch."""
        reg = WatchRegistry()
        answered = reg.add("accept everything", target="cockpit:shell", max_actions=2)
        untouched = reg.add("watch the build", target="cockpit:build", max_actions=2)
        reg.note_action("cockpit:shell")
        reg.note_action("cockpit:shell")
        assert reg.prune() == [answered]
        assert reg.active == [untouched]
        assert untouched.acted == 0

    def test_dropping_one_leaves_the_rest(self):
        reg = WatchRegistry()
        matched = reg.add("tell me when the build breaks", watch_for="FAILED")
        other = reg.add("answer claude")
        assert reg.drop(matched, "pattern matched")
        assert reg.active == [other]

    def test_the_pane_is_only_named_when_more_than_one_is_watched(self):
        """Otherwise it is noise in every prompt, every turn."""
        one = WatchRegistry()
        one.add("answer claude", target="cockpit:shell")
        assert "pane" not in one.describe()
        two = WatchRegistry()
        two.add("answer claude", target="cockpit:shell")
        two.add("watch the build", target="cockpit:build")
        assert "pane cockpit:build" in two.describe()


# Real `ps -t ttys004 -o pid=,ppid=,stat=,etime=,comm=` output from a pane with
# Claude Code running: fish forks, so claude is a *grandchild* of the pane pid,
# and its MCP servers and caffeinate share the foreground process group with it.
PS_CLAUDE = """\
67136 67135 Ss    09:05:41 fish
67138 67136 S     09:05:41 fish
67250 67138 S+    08:56:14 claude
67269 67250 S+    08:56:13 /Users/v/.claude/skills/grafana/.venv/bin/python
67270 67250 S+    08:56:13 /Users/v/.local/bin/codebase-memory-mcp
93493 67250 S+       03:22 caffeinate"""

PS_IDLE = """\
67136 67135 Ss    09:05:41 fish
67138 67136 Ss+   09:05:41 fish"""

# Claude mid-bash-tool: the child it spawned is foreground too, and tmux's
# pane_current_command reports the child rather than claude.
PS_CLAUDE_RUNNING_TOOL = PS_CLAUDE + "\n94001 67250 S+       00:02 git"


class TestProcessParsing:
    def test_program_name_skips_wrappers_and_env(self):
        assert program_name("FOO=1 uv run claude") == "claude"
        assert program_name("sudo vim /etc/hosts") == "vim"

    def test_program_name_reduces_a_path_to_its_basename(self):
        assert program_name("/Users/v/.venv/bin/python") == "python"

    @pytest.mark.parametrize(
        "etime,expected",
        [("03:22", 202), ("08:56:14", 32174), ("2-01:00:00", 176400), ("junk", 0)],
    )
    def test_etime_formats(self, etime, expected):
        assert parse_etime(etime) == expected

    def test_command_with_spaces_survives_the_split(self):
        procs = parse_ps("101 100 S+ 00:05 /opt/My Apps/thing")
        assert procs[0].raw == "/opt/My Apps/thing"

    def test_foreground_flag_comes_from_stat(self):
        procs = {p.pid: p for p in parse_ps(PS_CLAUDE)}
        assert procs[67250].foreground
        assert not procs[67136].foreground


class TestPaneOwner:
    """Identifying the program that owns the pane, with no stored state.

    Each test here is a case the old ForegroundStack got wrong.
    """

    def test_attaching_to_a_running_claude_finds_it(self):
        """The bug that motivated all of this: reattach used to report an idle
        shell, because nothing had pushed claude onto a stack."""
        owner = pane_owner(parse_ps(PS_CLAUDE))
        assert owner is not None
        assert owner.name == "claude"

    def test_owner_survives_the_shells_double_fork(self):
        """fish appears twice, so claude is a grandchild of the pane pid and a
        children-of-pid lookup would miss it."""
        procs = parse_ps(PS_CLAUDE)
        by_pid = {p.pid: p for p in procs}
        assert by_pid[67250].ppid == 67138 != 67136  # not a direct child
        assert pane_owner(procs).pid == 67250

    def test_true_age_is_reported_not_time_since_attach(self):
        """A stack pushed on attach would say "running 0s" for a 9-hour Claude."""
        owner = pane_owner(parse_ps(PS_CLAUDE))
        assert owner.age_secs == pytest.approx(32174)
        assert format_age(owner.age_secs) == "8.9h"

    def test_mcp_servers_and_caffeinate_are_not_the_owner(self):
        """All of them carry '+', so the foreground flag alone is not enough."""
        assert pane_owner(parse_ps(PS_CLAUDE)).name == "claude"

    def test_bash_tool_child_does_not_become_the_owner(self):
        """claude running `git status` is still claude owning the pane."""
        assert pane_owner(parse_ps(PS_CLAUDE_RUNNING_TOOL)).name == "claude"

    def test_idle_shell_has_no_owner(self):
        assert pane_owner(parse_ps(PS_IDLE)) is None

    def test_shell_is_identified(self):
        assert current_shell(parse_ps(PS_CLAUDE)) == "fish"


class TestObservationDescribe:
    def _seen(self, ps_output, **kwargs):
        procs = parse_ps(ps_output)
        return Observation(
            shell=current_shell(procs), cwd="~/repo",
            owner=pane_owner(procs), processes=procs, **kwargs
        )

    def test_idle_shell_says_so(self):
        assert "nothing running" in self._seen(PS_IDLE).describe()

    def test_running_claude_is_named_with_its_age(self):
        described = self._seen(PS_CLAUDE, full_screen=True).describe()
        assert "claude" in described
        assert "full-screen" in described
        assert "8.9h" in described

    def test_transient_child_is_reported_as_busyness_not_as_the_program(self):
        """The controller needs "claude, busy" — not "git"."""
        described = self._seen(
            PS_CLAUDE_RUNNING_TOOL, full_screen=True, busy_with="git"
        ).describe()
        assert described.count("›") == 1  # claude owns the pane, git is a detail
        assert "claude" in described
        assert "busy in git" in described

    def test_matching_busy_with_is_not_repeated(self):
        described = self._seen(PS_CLAUDE, busy_with="claude").describe()
        assert "busy in" not in described

    def test_dead_session_says_so(self):
        assert "no session running" in Observation(session_running=False).describe()

    def test_is_running_asks_the_process_list(self):
        seen = self._seen(PS_CLAUDE)
        assert seen.is_running("claude")
        assert not seen.is_running("vim")
        assert not seen.idle


class TestRenderedHistory:
    """Fed directly, with no tmux, so these stay fast and hermetic."""

    def _history(self, tmp_path):
        spool = tmp_path / "spool"
        spool.write_bytes(b"")
        return TerminalHistory(str(spool), cols=60, rows=6, max_lines=500)

    def test_scrolled_lines_are_retained(self, tmp_path):
        """capture-pane cannot do this; the whole point of the tap."""
        hist = self._history(tmp_path)
        hist.feed("".join(f"line {i}\r\n" for i in range(40)))
        text = hist.full()
        assert "line 0" in text
        assert "line 39" in text

    def test_repainted_frames_are_deduplicated(self, tmp_path):
        """Six spinner frames are one screen, not six."""
        hist = self._history(tmp_path)
        hist.feed("\x1b[?1049h")
        for i in range(6):
            hist.feed(f"\x1b[H\x1b[2JDialog here\r\n✳ Thinking… ({i}s · ↑ {i}00 tokens)")
        assert hist.in_alternate_screen
        assert len(hist._snapshots) == 1

    def test_real_change_makes_a_new_snapshot(self, tmp_path):
        hist = self._history(tmp_path)
        hist.feed("\x1b[?1049h")
        hist.feed("\x1b[H\x1b[2JFirst question")
        hist.feed("\x1b[H\x1b[2JSecond question")
        assert len(hist._snapshots) == 2

    def test_alternate_screen_is_swapped_not_merged(self, tmp_path):
        """pyte has no alt-screen buffer of its own; we supply one."""
        hist = self._history(tmp_path)
        hist.feed("shell output here\r\n")
        hist.feed("\x1b[?1049h\x1b[H\x1b[2JTUI OWNS THE SCREEN")
        assert hist.in_alternate_screen
        assert hist.tail() == "TUI OWNS THE SCREEN"
        hist.feed("\x1b[?1049l")
        assert not hist.in_alternate_screen
        # the shell's own output is still there underneath
        assert "shell output here" in hist.full()

    def test_final_tui_frame_is_kept_on_exit(self, tmp_path):
        hist = self._history(tmp_path)
        hist.feed("\x1b[?1049h\x1b[H\x1b[2JLast thing Claude said")
        hist.feed("\x1b[?1049l")
        assert "Last thing Claude said" in hist.full()

    def test_since_last_look_reports_quiet(self, tmp_path):
        hist = self._history(tmp_path)
        hist.feed("something happened\r\n")
        _, changed = hist.since_last_look("t")
        assert changed
        _, changed_again = hist.since_last_look("t")
        assert not changed_again

    def test_since_last_look_reports_only_the_new_lines(self, tmp_path):
        hist = self._history(tmp_path)
        hist.feed("first\r\n")
        hist.since_last_look("t")
        hist.feed("second\r\n")
        text, changed = hist.since_last_look("t")
        assert changed
        assert "second" in text
        assert "first" not in text

    def test_callers_track_their_own_position(self, tmp_path):
        hist = self._history(tmp_path)
        hist.feed("x\r\n")
        hist.since_last_look("agent")
        _, changed = hist.since_last_look("monitor")
        assert changed  # a different caller has not looked yet


class TestTurnBoundaryServicing:
    """The context block is what makes turn-boundary deciding work.

    The model has to learn, before it composes its reply, that a decision is
    pending — otherwise it answers the user and leaves the terminal blocked.
    """

    def _router(self, screen="", alt=False, running=True, ps_output=PS_IDLE):
        """A real AgentRouter with only the tmux- and ps-touching calls stubbed."""
        from agent_router import AgentRouter

        class StubbedRouter(AgentRouter):
            def _session_running(self):
                return running

            def pane_command(self, target=""):
                return "fish"

            def on_alternate_screen(self, target=""):
                return alt

            def current_directory(self, target=""):
                return "~/repo"

            def _read_processes(self, target=""):
                return parse_ps(ps_output)

            def capture_output(self, lines=None, target=""):
                return screen

        return StubbedRouter()

    def _block(self, screen="", alt=False, watch=None):
        router = self._router(screen=screen, alt=alt)
        if watch:
            router.watches.add(watch, target=router.default_target())
        return router.terminal_context_block()

    def test_no_watch_means_no_waiting_check(self):
        """Without a standing instruction, don't pay for a screen read each turn."""
        block = self._block(screen=DIALOG, alt=True)
        assert "[WAITING]" not in block
        assert "[WATCHING]" not in block

    def test_watch_is_reported_each_turn(self):
        block = self._block(screen="$ ", watch="accept defaults")
        assert "[WATCHING]" in block
        assert "accept defaults" in block

    def test_pending_question_is_surfaced_with_the_watch(self):
        block = self._block(screen=DIALOG, alt=True, watch="accept defaults")
        assert "[WAITING]" in block
        assert "Do you want to proceed?" in block
        # the options must survive, since the model picks among them
        assert "2. Yes, and don't ask again" in block

    def test_quiet_terminal_reports_no_waiting(self):
        block = self._block(screen="$ ", watch="accept defaults")
        assert "[WATCHING]" in block
        assert "[WAITING]" not in block

    def test_dead_session_says_so(self):
        block = self._router(running=False).terminal_context_block()
        assert "no session running" in block

    def test_every_watched_pane_is_checked_for_a_question(self):
        """A question on the second pane was invisible at the moment to answer it."""
        router = self._router()
        router.capture_output = lambda lines=None, target="": (
            REAL_BASH_DIALOG if target == "cockpit:build" else "$ "
        )
        router.observe = lambda target="": Observation(
            shell="fish", cwd="~/repo", owner=None, busy_with="fish",
            full_screen=(target == "cockpit:build"),
        )
        router.watches.add("answer claude", target=router.default_target())
        router.watches.add("watch the build", target="cockpit:build")
        block = router.terminal_context_block()
        assert "[WAITING] Pane cockpit:build is asking" in block
        assert "Do you want to proceed?" in block


class _StubContext:
    def __init__(self):
        self.messages = []

    def add_message(self, message):
        self.messages.append(message)


class _StubTask:
    def __init__(self):
        self.frames = []

    async def queue_frames(self, frames):
        self.frames.extend(frames)


class _StubRuntime:
    """Just enough AgentRuntime for the escalation hop."""

    def __init__(self, active="controller", tools=("capture_output",)):
        self.active_agent_id = active
        self.controller_agent_id = "controller"
        self._tools = {"controller": list(tools), "shell": ["send_keys", "run_command"]}
        self.applied = []
        self.models_applied = []

    class _Spec:
        def __init__(self, agent_id, tool_names):
            self.agent_id = agent_id
            self.tool_names = tool_names

    @property
    def active_spec(self):
        return self._Spec(self.active_agent_id, self._tools[self.active_agent_id])

    def agent_for_tool(self, tool_name):
        if tool_name in self.active_spec.tool_names:
            return self.active_agent_id
        return next(
            (a for a, t in self._tools.items() if tool_name in t and a != "controller"), ""
        )

    def apply_agent(self, agent_id, **kwargs):
        self.applied.append(agent_id)
        self.active_agent_id = agent_id

    async def apply_agent_model(self, agent_id, **kwargs):
        self.models_applied.append(agent_id)


class _StubRouter:
    """A router that only has to answer the questions the monitor asks."""

    def __init__(self, screen="$ ", alt=False, quiet=True):
        from terminal_tasks import WatchRegistry

        self.watches = WatchRegistry()
        self._screen = screen
        self._alt = alt
        self._quiet = quiet
        self.captured = []
        # Panes that exist. A watch on a target outside this set is the
        # "invented pane name" case.
        self.panes = {"cockpit:shell", "cockpit:build"}

    def default_target(self):
        return "cockpit:shell"

    def resolve_target(self, target=""):
        return target or "cockpit:shell"

    def pane_exists(self, target=""):
        return (target or "cockpit:shell") in self.panes

    def capture_output(self, lines=None, target=""):
        self.captured.append(target)
        return self._screen

    def on_alternate_screen(self, target=""):
        return self._alt

    def seconds_since_user_spoke(self):
        return 999.0 if self._quiet else 0.0


class TestMonitorRegistry:
    """One loop per watched pane, so a second watch is not simply refused."""

    def _monitor(self, **kwargs):
        return TerminalMonitor(_StubRouter(**kwargs), _StubTask(), _StubContext())

    def test_a_second_pane_gets_its_own_loop(self):
        async def go():
            mon = self._monitor()
            mon.start(target="cockpit:shell")
            mon.start(target="cockpit:build")
            watched = sorted(mon.watched)
            mon.stop()
            return watched

        assert asyncio.run(go()) == ["cockpit:build", "cockpit:shell"]

    def test_watching_the_same_pane_twice_joins_rather_than_refuses(self):
        """The old message was "Already watching", and the instruction was orphaned."""
        async def go():
            mon = self._monitor()
            mon.start(target="cockpit:shell")
            second = mon.start(target="cockpit:shell")
            loops = len(mon.watched)
            mon.stop()
            return second, loops

        second, loops = asyncio.run(go())
        assert "joins it" in second
        assert loops == 1

    def test_stopping_one_pane_leaves_the_other_running(self):
        async def go():
            mon = self._monitor()
            mon.start(target="cockpit:shell")
            mon.start(target="cockpit:build")
            mon.stop("asked to stop", "cockpit:shell")
            watched = mon.watched
            mon.stop()
            return watched

        assert asyncio.run(go()) == ["cockpit:build"]

    def test_a_pattern_match_retires_only_its_own_instruction(self):
        """Matching "FAILED" must not also stop the watch answering Claude Code."""
        async def go():
            router = _StubRouter(screen="build FAILED\n$ ")
            mon = TerminalMonitor(router, _StubTask(), _StubContext())
            build = router.watches.add(
                "tell me when it breaks", watch_for="FAILED", target="cockpit:shell"
            )
            answering = router.watches.add("answer claude", target="cockpit:shell")
            mon.start(interval_secs=0.01, target="cockpit:shell")
            await asyncio.sleep(0.1)
            still_watching = mon.watched
            mon.stop()
            return router.watches.active, still_watching, build, answering

        active, still_watching, build, answering = asyncio.run(go())
        assert active == [answering]
        assert build not in active
        assert still_watching == ["cockpit:shell"]

    def test_a_watch_on_a_pane_that_does_not_exist_stops_itself(self):
        """A target is just a string, so the model can invent one ("main").

        That watch then failed on every poll for the life of the process,
        logging a tmux error a second and outliving the session entirely.
        """
        async def go():
            router = _StubRouter()
            router.watches.add("watch the build", target="main")
            mon = TerminalMonitor(router, _StubTask(), _StubContext())
            mon.start(interval_secs=0.01, target="main")
            await asyncio.sleep(0.1)
            return mon.watched, router.watches.active, router.captured

        watched, active, captured = asyncio.run(go())
        assert watched == []
        assert active == []
        assert captured == []  # it never even tried to read the missing pane

    def test_the_loop_ends_when_its_pane_has_no_instructions_left(self):
        async def go():
            router = _StubRouter()
            mon = TerminalMonitor(router, _StubTask(), _StubContext())
            mon.start(interval_secs=0.01, target="cockpit:shell")
            await asyncio.sleep(0.1)
            return mon.watched

        assert asyncio.run(go()) == []

    def test_each_pane_is_polled_by_its_own_target(self):
        async def go():
            router = _StubRouter()
            router.watches.add("watch the build", target="cockpit:build")
            mon = TerminalMonitor(router, _StubTask(), _StubContext())
            mon.start(interval_secs=0.01, target="cockpit:build")
            await asyncio.sleep(0.05)
            mon.stop()
            return router.captured

        captured = asyncio.run(go())
        assert captured
        assert set(captured) == {"cockpit:build"}


class TestEscalationHop:
    """A wake that needs a key pressed must land on an agent that can press one."""

    def _wake(self, reason, runtime, announce=None):
        async def go():
            router = _StubRouter()
            mon = TerminalMonitor(
                router, _StubTask(), _StubContext(), runtime=runtime, announce=announce
            )
            await mon._wake(reason, "screen", "cockpit:shell")

        asyncio.run(go())

    def test_a_pending_question_switches_to_an_agent_with_hands(self):
        runtime = _StubRuntime(active="controller")
        self._wake(WakeReason.PROMPT, runtime)
        assert runtime.applied == ["shell"]
        assert runtime.active_agent_id == "shell"

    def test_the_new_agents_model_comes_with_it(self):
        """Switching agent without it ran the shell prompt on the controller's model."""
        runtime = _StubRuntime(active="controller")
        self._wake(WakeReason.PROMPT, runtime)
        assert runtime.models_applied == ["shell"]

    def test_a_report_does_not_switch_agent(self):
        """FINISHED and EXPIRED need no hands; the controller can say them."""
        runtime = _StubRuntime(active="controller")
        self._wake(WakeReason.FINISHED, runtime)
        assert runtime.applied == []

    def test_an_agent_that_can_already_act_is_left_alone(self):
        runtime = _StubRuntime(active="shell")
        self._wake(WakeReason.PROMPT, runtime)
        assert runtime.applied == []

    def test_no_runtime_still_wakes(self):
        """The hop is an improvement on the wake, not a precondition for it."""
        async def go():
            task = _StubTask()
            context = _StubContext()
            mon = TerminalMonitor(_StubRouter(), task, context)
            await mon._wake(WakeReason.PROMPT, "screen", "cockpit:shell")
            return task.frames, context.messages

        frames, messages = asyncio.run(go())
        assert frames and messages

    def test_a_failed_escalation_does_not_swallow_the_wake(self):
        class Broken(_StubRuntime):
            def apply_agent(self, agent_id, **kwargs):
                raise RuntimeError("registry is in a bad way")

        async def go():
            task = _StubTask()
            mon = TerminalMonitor(
                _StubRouter(), task, _StubContext(), runtime=Broken()
            )
            await mon._wake(WakeReason.PROMPT, "screen", "cockpit:shell")
            return task.frames

        assert asyncio.run(go())


class TestWakeIsAnnounced:
    """A monitor report is the one message that can arrive unnoticed."""

    def _wake(self, reason):
        announced = []

        async def announce(reason_name, text):
            announced.append((reason_name, text))

        async def go():
            mon = TerminalMonitor(
                _StubRouter(), _StubTask(), _StubContext(), announce=announce
            )
            await mon._wake(reason, "screen", "cockpit:build")

        asyncio.run(go())
        return announced

    def test_every_wake_is_announced_on_its_own_channel(self):
        """Text-only mode marks replies skip_tts; a quiet spell mutes the mic."""
        assert self._wake(WakeReason.PROMPT) == [
            ("prompt", "Terminal: prompt (pane cockpit:build)")
        ]

    def test_a_finished_report_is_announced_too(self):
        assert self._wake(WakeReason.FINISHED)[0][0] == "finished"

    def test_a_broken_announcer_does_not_swallow_the_wake(self):
        async def announce(reason_name, text):
            raise RuntimeError("transport is gone")

        async def go():
            task = _StubTask()
            mon = TerminalMonitor(
                _StubRouter(), task, _StubContext(), announce=announce
            )
            await mon._wake(WakeReason.PROMPT, "screen", "")
            return task.frames

        assert asyncio.run(go())
