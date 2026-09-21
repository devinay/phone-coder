# Captured terminal fixtures

Real `tmux capture-pane -p` output, not constructed. The dialog geometry in
`terminal_monitor.find_prompt` was sized against a hand-written dialog, which is
a way of testing that the code agrees with itself; these are what Claude Code
actually draws.

Captured from a 100x30 tmux pane running `claude --model haiku`:

| File | Dialog | Question's distance from the bottom, in non-blank lines |
| --- | --- | --- |
| `claude-code-bash-permission.txt` | Bash tool, 3 options | 5 |
| `claude-code-edit-permission.txt` | Edit tool, 3 options, one wrapping onto a second line, diff above | 6 |
| `claude-code-multiselect.txt` | `AskUserQuestion` with `multiSelect`, 3 checkbox options | 6 |
| `claude-code-multiselect-review.txt` | The review screen `Right` leads to — an ordinary radio list | — |

Two things these settled that the constructed dialog could not:

- **The window is wide enough.** `_PROMPT_WINDOW` is 15, so the worst observed
  case clears it by 9 lines.
- **The window was the wrong thing to return.** The edit dialog fills the whole
  screen — the diff being approved is above the question, and scrolls off the
  top. Returning only the detection window handed the model a question about a
  change it could not see, which is why `find_prompt` now detects narrow and
  returns wide (`_PROMPT_CONTEXT`).

## The multi-select trap

`claude-code-multiselect.txt` is a different widget wearing the same clothes. It
is a numbered list like every other dialog, but the keys mean different things,
and nothing on screen says so — the footer reads "Enter to select", which is
true and misleading at once.

Driving the real dialog established what actually happens:

| Key | Effect |
| --- | --- |
| a number | toggles that option; the cursor does not move and the dialog stays open |
| `Enter` | toggles the highlighted row — it does **not** submit |
| `Right` | moves to the Submit tab, showing a review screen |
| `1` on the review screen | submits (that screen is an ordinary radio list) |

So the sequence is: numbers for what you want, `Right`, then `1`.

This is what the agent got stuck on in a real session — it pressed `1`, nothing
advanced, it tried the arrows, the highlight moved to "Type something", and the
user had to finish by hand. `is_multi_select` tells the two apart on the `[ ]`
option marker, and `how_to_answer` returns the sequence, surfaced as a
`[HOW TO ANSWER IT]` line beside the screen.

To recapture after a Claude Code release changes the layout:

```
tmux new-session -d -s dlgcap -x 100 -y 30 -c <some scratch dir> "claude --model haiku"
tmux send-keys -t dlgcap -l "run the shell command: touch /tmp/perm-probe.txt"
tmux send-keys -t dlgcap Enter
tmux capture-pane -p -t dlgcap > claude-code-bash-permission.txt
```

For the multi-select, ask Claude Code to use the tool directly:

```
tmux send-keys -t dlgcap -l "Use the AskUserQuestion tool: ask which deploy targets, multiSelect true, options staging, prod, canary"
tmux send-keys -t dlgcap Enter
```
