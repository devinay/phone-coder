# Terminal context plan

> **Status: implemented** on branch `terminal-context`, checkpoint to fall back
> to is `ccdaaf1`. New modules: `terminal_vocab.py`, `terminal_screen.py`,
> `terminal_history.py`, `terminal_state.py`. Tests in
> `tests/test_terminal_context.py` (44 cases). See "What shipped" at the end.


Making the controller and shell agents actually aware of the terminal: what is
running in it, what it has said, and what it is currently asking.

## The four problems

**A. Mishearing "claude" as "cloud".** `bot.py:298` constructs Deepgram with
`LiveOptions(diarize=False, punctuate=True, smart_format=True)` — no keyterm or
vocabulary hint. The Grove path (`bot.py:288`) passes none either. Nova has no
reason to prefer a proper noun it has never been told about. The controller is
routing the word it was handed; this is an STT problem, not an LLM one.

**B. The controller cannot see the terminal.** Its tool list
(`agents/controller/prompt.md:26-32`) is admin and routing only, and its prompt
never establishes that a live shell exists as a thing with contents — only that
terminal requests should be routed to `shell`.

**C. Terminal observations are destroyed on handoff.** `apply_agent` calls
`_strip_tool_call_messages` (`agents/runtime.py:473`, defined at `823`), which
drops tool calls *and their results*. The captured screen, and the fact that
`run_command("claude")` succeeded, are gone at the next agent switch. Nothing
else records them — there is no session state, only tool results that get swept.
This is the direct cause of "it does not know that claude is launched".

**D. The answering path is broken in three independent ways.** Detailed in
section 5 below. Even with A–C fixed, "answer yes as needed" would still fail.

## Findings that constrain the design

Both established empirically in this session, not assumed.

**`pipe-pane` does capture full-screen output — but not as lines.** Running an
alternate-screen program under `tmux pipe-pane` produced:

```
^[[?1049h^[[HSPINNER frame 0^[[HSPINNER frame 1^[[HSPINNER frame 2^[[?1049l
```

Claude's output *will* be in the log; nothing is hidden. But five distinct
screen frames occupy one physical line, each overwriting the last via `\033[H`.
Line count therefore has no relationship to how much history exists: an hour of
Claude Code may be a few dozen physical lines of superimposed frames, while one
second of spinner may be thousands. **A 1000-line tail is not a meaningful unit
for TUI output.** The ring must hold rendered screens, not log lines.

**`pane_current_command` and `alternate_on` both track cleanly.** On tmux 3.7b,
across a `less` invocation: `bash/0 → less/1 → bash/0`. These are reliable
enough to drive the foreground stack.

## 1. Fix the hearing

- Add `keyterm=[...]` to `LiveOptions` in `bot.py:298`: `claude`, `codex`,
  `tmux`, `fish`, `git`, `grep`, `xargs`, plus frequently-used repo names.
- Add a transcript normalisation step ahead of the LLM: `cloud`/`clod`/`claud`
  → `claude` **only** when adjacent to a launch or terminal verb (launch, run,
  start, open, ask, tell, in the terminal). Unconditional replacement would
  break genuine cloud-infrastructure sentences.
- Grove path: pass the same terms if the endpoint supports a prompt/vocabulary
  field; if it does not, the normalisation step still covers it.

Cheapest change with the largest effect on perceived competence.

## 2. Rendered-screen ring buffer

At session start, `tmux pipe-pane -o -t <target> 'cat >> <spool>'`. Feed that
stream into a `pyte` screen (new dependency — not currently installed) that
maintains a virtual 80×24 terminal exactly as tmux does. Two tiers, because the
two kinds of output have genuinely different shapes:

- **Line-oriented output** (fish, git, builds — scrolls, never repaints): a
  1000-line ring, as originally proposed. The log really is lines here.
- **Full-screen output** (Claude, vim): a ring of *rendered snapshots*, a new
  entry appended only when the normalised screen differs from the previous one.
  Bounded by total size rather than line count.

This buys the "knows what changed, or that nothing did" property directly:
*nothing changed* becomes a cheap, truthful answer (no new ring entry since the
last look), and *what changed* is a diff of the last two snapshots — far fewer
tokens than resending a whole screen.

New read-only tools: `terminal_since_last_look()` returning the diff or
`unchanged`, alongside the existing `capture_output`.

### How pyte fits

`pyte` is a headless terminal emulator: a `Screen` (an 80x24 grid of cells) plus
a `Stream` that parses ANSI escapes and mutates the grid — the same job tmux
does internally, but in-process and inspectable. Feed it bytes, read back
`screen.display` as 24 plain strings.

```
pipe-pane spool --bytes--> pyte.Stream --> pyte.Screen --> screen.display
                          (parses ESC[H,                  (clean text lines)
                           ESC[2J, SGR...)
```

Verified end to end on a captured stream (5524 bytes / 187 physical lines of
superimposed frames): pyte rendered it back to the exact screen a human sees,
the boxed dialog intact. Measured on the same stream:

| dedup method                        | "changed" events |
| ----------------------------------- | ---------------- |
| exact compare (current design)      | 8 of 8 frames    |
| spinner line masked (proposed)      | 4                |

Within the spinner run, masked compare collapses to no change at all — which is
exactly the 5c fix. Prompt detection over the rendered screen with borders
stripped: `window=3` MISSED, `window=15` FOUND, confirming 5a against rendered
output rather than a hand-written mock.

### Known pyte limitation: no alternate screen buffer

pyte does **not** implement `?1049h`/`?1049l`. Verified directly:

```
feed: BEFORE-ALT -> ?1049h -> INSIDE-TUI -> ?1049l
real terminal shows: BEFORE-ALT   (restored)
pyte shows:          INSIDE-TUI   (never restored)
1049 in screen.mode: False
```

So on Claude Code exit, pyte would keep the stale dialog on screen forever
instead of revealing the shell scrollback beneath it.

Fix: detect `?1049h`/`?1049l` in the stream ourselves and swap between two
`Screen` instances — a primary for shell output, an alternate for the TUI. This
is not just a workaround: that same sequence is the natural signal for which of
the two ring tiers output belongs to, and a third independent confirmation of
`alternate_on` for the foreground stack in section 3.

**Lifetime.** The ring lives in memory, so "deleted on disconnect" is free —
no file to leak if the process dies badly. The on-disk spool exists only
because `pipe-pane` needs a sink; create it under the session directory and
unlink it in the same teardown that kills the tmux session
(`agent_router.py:231`).

*Accepted tradeoff:* an in-memory ring loses history across a bot restart even
when the tmux session survives. Reattaching to a mid-flight Claude session is a
separate feature; not solving it here.

## 3. Foreground stack

Replaces the flat status line with a stack, as requested. Each entry:
`{program, cwd, launched_at, launched_by_tool}`.

- **Push** when `run_command` launches something interactive.
- **Pop** when it exits, gated on *two* signals: `pane_current_command`
  returning to the shell, and `alternate_on` flipping back to `0`.

The critical subtlety: Claude Code spawns its own subprocesses for its bash
tool, so `pane_current_command` will transiently report something other than
`claude` **without Claude having exited**. Pops must therefore be gated on
returning to the *shell* specifically, never on any change — otherwise the
stack pops Claude off every time it runs a tool. The stack is authoritative for
what we launched; tmux is the reconciler for whether it is still there.

Renders as one compact line per turn:
`[TERMINAL] fish ~/mongo-src/foo › claude (running 4m, full-screen)`

Because it lives on the router rather than in the message list,
`_strip_tool_call_messages` cannot erase it. That is the structural fix for C.

## 4. Controller access

Through the ring, read-only. Not the raw spool.

1. Inject the stack's status line for **every** agent, controller included, on
   every turn. "Doesn't know claude is launched" becomes impossible: it is
   unconditionally in the prompt rather than something to remember from a
   stripped tool result.
2. Grant the controller `capture_output` and `terminal_since_last_look` only.
   Keep `run_command`, `send_input`, `send_key`, and the monitor in `shell`.
   The controller can *see* the terminal and speak about it without being able
   to type into it — routing discipline preserved, blindness removed.
3. Add a prompt section to `agents/controller/prompt.md`: one live fish shell in
   tmux, full unix toolset available, `claude` and `codex` are interactive
   full-screen programs launched through it.

## 5. The answering path

Three independent defects, all in answering rather than plumbing.

### 5a. It cannot see the question

`find_prompt` (`terminal_monitor.py:89`) examines only the last **3** non-empty
lines. A Claude Code permission dialog ends with option 2, option 3, and the
box's bottom border, putting `Do you want to proceed?` and `❯ 1. Yes` four to
five lines up — outside the window. Run against a representative dialog, the
function returns `''`, so the monitor concludes nothing is waiting. The
`❯\s*1\.\s*yes` pattern at `terminal_monitor.py:30` was written for this dialog
and can never match with a 3-line tail.

Fix: match over ~15 lines with box-drawing characters (`│ ╭ ╰ ─ ╮ ╯`) stripped
first, so text inside the border is visible as text.

Note the window must stay *narrow*. This is a liveness check, not a history
lookup — at 500 lines it would match questions that scrolled past and were
already answered, then send "yes" into whatever now holds the keyboard. ~15 is
the target, tightened rather than widened.

*To verify during implementation:* the exact dialog geometry above is inferred,
not captured from a live Claude Code session. Capture a real dialog first and
size the window to it.

### 5b. It sends the wrong keystrokes

`_answer` (`terminal_monitor.py:202`) calls `send_input("yes")`, and
`send_input` (`agent_router.py:106-113`) unconditionally appends `C-m`. But the
Claude Code dialog is a *numbered selector* — it wants `1`, or a bare Enter to
accept the highlighted option. Typing the letters `y-e-s` into it does nothing.
There is currently no way to send a bare keypress at all: no Enter-alone, no
digits, no arrows, no Escape.

Fix: add `send_key(keys)` to the router — `send-keys -t <target> <keys>` with no
trailing `C-m`, so `1`, `Enter`, `Up`, `Escape` all work. Have `_answer` choose
`1` for a numbered menu and `y` for a `(y/n)` prompt. Keep `send_input` for
prose prompts.

### 5c. Waiting for completion never completes

`wait_for_idle` and `watch` (`agent_router.py:178-190`, `212-229`) decide
"settled" by comparing consecutive screens as exact strings. Claude Code
redraws a spinner and a live elapsed/token counter, so the screens differ on
essentially every poll: the idle branch never fires and the tool burns its full
120s or 300s timeout before returning "still changing". The monitor's
`screen != settled` check at `terminal_monitor.py:275` has the same flaw, and
its `is_finished` looks for a shell prompt that never appears while a TUI owns
the pane.

Fix: normalise before comparing, masking the status/spinner region, so "only
the counter ticked" reads as unchanged. This is the same normalisation the ring
buffer needs for dedup — one piece of work serving both.

This is why things *feel* hung rather than wrong, and it is independent of the
ring buffer.

## Order

1. **1** — STT keyterms and normalisation. Standalone, immediate payoff.
2. **5c** — screen normalisation. The ring depends on it for dedup.
3. **2** — pyte-backed ring buffer and diff tools.
4. **3** — foreground stack.
5. **4** — controller read access and prompt.
6. **5a / 5b** — dialog detection and `send_key`.

## What shipped

| Section | Module / file | Verified by |
| --- | --- | --- |
| 1 STT | `terminal_vocab.py`, `TranscriptNormaliser`, Deepgram `keyterm` | 17 vocabulary cases |
| 5c idle | `terminal_screen.py`, wired into `wait_for_idle`/`watch`/monitor | spinner frames compare equal; real changes still seen |
| 2 history | `terminal_history.py` (pyte, alt-screen swap, two tiers) | 62 lines of scrollback retained; 6 TUI frames deduped to 2 |
| 3 stack | `terminal_state.py` `ForegroundStack` | claude survives its own subprocesses; drains only at a shell |
| 4 controller | status injector, read-only tools, prompt sections | registry/frontmatter agree |
| 5a/5b answering | `find_prompt` window 15 + borders, `router.send_key` | dialog found; keypress `1` lands |

Integration test against a real tmux pane and a real TUI: 16/16 checks.

## Known gaps, not built

Deliberately out of scope for this pass; each is a separate piece of work.

**One watch at a time.** `TerminalMonitor.start` refuses a second monitor. "Watch
claude and also watch the build" needs a monitor registry keyed by what is being
watched, plus per-watch policy.

**The escalation hop is prompt-only.** When the monitor escalates, the LLM wakes
as whatever agent is active — normally the controller, because
`AgentTurnResetter` returns there after every worker turn
(`agents/runtime.py:640`). The controller can now *see* the terminal but still
cannot press a key, so answering requires it to activate `shell`. That hop is
described in the prompts rather than enforced in code; a monitor that could
request `shell` for its own escalation turn would be sturdier.

**Async notifications may be silent.** `TTSGate` marks response text `skip_tts`
when browser voice mode is off, and idle-mute can be in effect, so a monitor
report can arrive on screen without being spoken.

**Dialog geometry is inferred.** The 15-line window was sized against a
constructed dialog, not a captured Claude Code session. Capture a real one and
confirm the question still falls inside the window.
