"""Tests for terminal awareness: vocabulary, screen normalisation, foreground
state, prompt detection and rendered history.

Each group pins down a bug that made "watch Claude and answer yes" fail.
"""

import pytest

from terminal_history import TerminalHistory
from terminal_monitor import find_prompt, is_finished
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

            def pane_command(self):
                return "fish"

            def on_alternate_screen(self):
                return alt

            def current_directory(self):
                return "~/repo"

            def _read_processes(self):
                return parse_ps(ps_output)

            def capture_output(self, lines=None):
                return screen

        return StubbedRouter()

    def _block(self, screen="", alt=False, watch=None):
        router = self._router(screen=screen, alt=alt)
        if watch:
            router.watches.add(watch)
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
