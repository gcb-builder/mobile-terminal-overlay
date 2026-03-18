#!/bin/bash
# Kill all existing MTO instances and start fresh
pids=$(pgrep -f "mobile-terminal" | grep -v $$)
if [ -n "$pids" ]; then
    echo "Killing: $pids"
    echo "$pids" | xargs kill -9 2>/dev/null
    sleep 0.5
fi
nohup /home/gcbbuilder/dev/mobile-terminal-overlay/venv/bin/mobile-terminal --session claude --port 9000 --base-path /terminal --no-auth --host 0.0.0.0 --verbose > /tmp/mto.log 2>&1 &
echo "Started PID: $!"
