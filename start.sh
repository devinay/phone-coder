#!/bin/bash
# Start the Voice Coding Cockpit.
#
# tmux layout:
#   Left pane  (0.0): shell — visible via ttyd in the browser
#   Right pane (0.1): bot server (uv run bot.py)
#
# Browser:
#   http://localhost:7860/cockpit  — cockpit UI (chat + embedded terminal)
#   ttyd streams the tmux shell pane to the cockpit iframe on port 7681

SESSION="cockpit"
ROOT="$(cd "$(dirname "$0")" && pwd)"
DIR="$ROOT/server"

# Kill existing session and ttyd if running
tmux kill-session -t $SESSION 2>/dev/null
pkill -f "ttyd.*$SESSION" 2>/dev/null

# New session — window 0, left pane = shell
tmux new-session -d -s $SESSION -c "$ROOT"

# Right pane = bot server
tmux split-window -h -t "${SESSION}:0.0" -c "$DIR"

# Left pane (0.0): shell, ready for agent work
tmux send-keys -t "${SESSION}:0.0" "echo 'Shell ready'" Enter

# Right pane (0.1): bot server
tmux send-keys -t "${SESSION}:0.1" "uv run bot.py 2> >(grep -v '^objc\[' >&2)" Enter

# Start ttyd — streams the shell pane (0.0) to the browser on port 7681
# writable=1 so you can type in the embedded terminal
ttyd --port 7681 --writable tmux attach-session -t "${SESSION}" &
TTYD_PID=$!
echo "ttyd started (pid $TTYD_PID) — terminal available at http://localhost:7681"

echo ""
echo "Open http://localhost:7860/cockpit in your browser."
echo "Press Ctrl-C here or 'q' to stop everything."
echo ""

# Attach to tmux (blocks until detached)
tmux attach -t $SESSION

# Cleanup ttyd when tmux session ends
kill $TTYD_PID 2>/dev/null
