#!/bin/bash
# Start the Voice Coding Cockpit in a 2-pane tmux layout.
# Left pane: bot server (uv run bot.py)
# Right pane: shell (for agents, git, tests, etc.)

SESSION="cockpit"

# Kill existing session if present
tmux kill-session -t $SESSION 2>/dev/null

# New session, start in server directory, left pane = bot server
tmux new-session -d -s $SESSION -c "$(dirname "$0")/server"

# Split vertically — right pane = shell, same directory
tmux split-window -h -t $SESSION -c "$(dirname "$0")/server"

# Left pane: start the bot server
tmux send-keys -t $SESSION:0.0 "uv run bot.py" Enter

# Right pane: just a shell prompt, ready for agent work
tmux send-keys -t $SESSION:0.1 "echo 'Shell ready. Open http://localhost:7860/cockpit in your browser.'" Enter

# Attach
tmux attach -t $SESSION
