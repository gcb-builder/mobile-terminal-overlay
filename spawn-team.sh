#!/usr/bin/env bash
set -e

# spawn-team.sh - Create agent team with worktree-safe branches
#
# Usage:
#   ./spawn-team.sh [session] [agent1] [agent2] ...
#   ./spawn-team.sh claude eval back frontend
#   ./spawn-team.sh              # defaults: session=claude, agents=eval back
#
# Cleanup:
#   ./spawn-team.sh --kill [session]

# --- Handle --kill mode ---
if [[ "$1" == "--kill" ]]; then
    SESSION="${2:-claude}"
    echo "Killing team windows in session $SESSION..."
    for win in $(tmux list-windows -t "$SESSION" -F '#{window_name}'); do
        if [[ "$win" == "leader" || "$win" == a-* ]]; then
            echo "  Killing $win"
            tmux kill-window -t "$SESSION:$win" 2>/dev/null || true
        fi
    done
    echo "Done."
    exit 0
fi

SESSION="${1:-claude}"
shift 2>/dev/null || true
AGENTS=("$@")

# Default agents if none specified
if [ ${#AGENTS[@]} -eq 0 ]; then
    AGENTS=("eval" "back")
fi

# --- Guardrails ---

# Ensure tmux session exists
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Error: tmux session '$SESSION' does not exist."
    exit 1
fi

# Ensure inside git repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: not inside a git repo."
    exit 1
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
CURRENT_BRANCH=$(git branch --show-current)

# Prevent spawning from main/master
if [[ "$CURRENT_BRANCH" == "main" || "$CURRENT_BRANCH" == "master" ]]; then
    BASE_BRANCH="feature/team-$(date +%Y%m%d-%H%M%S)"
    echo "On $CURRENT_BRANCH - creating base branch: $BASE_BRANCH"
    git checkout -b "$BASE_BRANCH"
else
    BASE_BRANCH="$CURRENT_BRANCH"
fi

echo "Base branch: $BASE_BRANCH"
echo "Repo: $REPO_ROOT"
echo "Session: $SESSION"
echo ""

spawn_window() {
    local NAME="$1"

    # Skip if window already exists
    if tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -qx "$NAME"; then
        echo "  $NAME - already exists, skipping"
        return
    fi

    tmux new-window -a -t "$SESSION" -n "$NAME" -c "$REPO_ROOT"

    # Create unique branch per agent (or switch to it if exists)
    local AGENT_BRANCH="${BASE_BRANCH}-${NAME}"
    tmux send-keys -t "$SESSION:$NAME" \
        "git checkout -b ${AGENT_BRANCH} 2>/dev/null || git checkout ${AGENT_BRANCH}" Enter

    # Wait for checkout to complete before starting claude
    sleep 0.5

    tmux send-keys -t "$SESSION:$NAME" "claude --worktree" Enter

    echo "  $NAME -> branch $AGENT_BRANCH"
}

echo "Spawning team:"
spawn_window "leader"
for agent in "${AGENTS[@]}"; do
    spawn_window "a-${agent}"
done

echo ""
echo "Team ready. Windows:"
echo "  leader"
for agent in "${AGENTS[@]}"; do
    echo "  a-${agent}"
done
