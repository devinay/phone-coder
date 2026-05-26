#!/bin/bash
# Start the Voice Coding Cockpit in a 2-pane tmux layout.
# Left pane: bot server (uv run bot.py)
# Right pane: shell (for agents, git, tests, etc.)

SESSION="cockpit"
DIR="$(cd "$(dirname "$0")" && pwd)/server"

# Kill existing session if present
tmux kill-session -t $SESSION 2>/dev/null

# New session — window 0, left pane (pane 0)
tmux new-session -d -s $SESSION -c "$DIR"

# Split window 0 horizontally — creates right pane (pane 1)
tmux split-window -h -t "${SESSION}:0.0" -c "$DIR"

# Left pane: bot server
tmux send-keys -t "${SESSION}:0.0" "uv run bot.py" Enter

# Right pane: shell prompt
tmux send-keys -t "${SESSION}:0.1" "echo 'Shell ready — open http://localhost:7860/cockpit'" Enter

# Attach
tmux attach -t $SESSION
