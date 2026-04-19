#!/usr/bin/env bash
#
# Rebuild the MTO frontend bundle and restart the user-systemd service
# so the browser sees the new JS / CSS / HTML on the next load.
#
# Why this exists: ``systemctl --user restart mto`` alone does NOT
# rebuild the frontend — it just re-execs the Python process against
# whatever is in ``mobile_terminal/static/dist/``. If that bundle is
# from a prior build, every frontend edit sits invisible until someone
# manually runs ``npm run build``. Same trap secondbrain'\''s
# scripts/deploy-web.sh exists to prevent.
#
# Runs:
#   1. Python sanity check (import the package — fails on syntax errors)
#   2. Frontend build (sync-version + esbuild)
#   3. Restart mto user service
#   4. Smoke-test: GET /health returns 200, GET /api/ws-debug returns 200
#
# Fails fast — a broken Python import or build aborts before the
# restart, leaving the existing service untouched. You see the error
# and the previous bundle keeps serving.
#
# Does NOT touch tmux-claude.service. The tmux session is independent
# of the MTO web service; killing tmux would close every Claude pane,
# which is never what a frontend redeploy wants. Restart that one
# manually if a backend change to PTY handling makes it necessary.
#
# Usage:
#   bash scripts/deploy.sh                    # build + restart + smoke test
#   bash scripts/deploy.sh --no-restart       # build only, leave service alone
#   bash scripts/deploy.sh --skip-build       # restart + smoke test only

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Tunables — change these if your install layout differs.
SERVICE_NAME="mto"                                         # systemd --user unit name
# The server registers API/health routes at ROOT — base_path (/terminal)
# is only used for serving static assets and the index page, plus by
# the upstream proxy (Caddy / Tailscale serve) for path-prefix routing.
# Smoke tests hit the server directly on 127.0.0.1:8080 with no prefix.
HEALTH_URL="http://127.0.0.1:8080/health"
DEBUG_URL="http://127.0.0.1:8080/api/ws-debug"
PYTHON_VENV="$REPO_ROOT/venv/bin/python3"

# Flag parsing
DO_BUILD=1
DO_RESTART=1
for arg in "$@"; do
    case "$arg" in
        --no-restart) DO_RESTART=0 ;;
        --skip-build) DO_BUILD=0 ;;
        -h|--help)
            sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# //;s/^#$//'
            exit 0
            ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

echo "=== Deploy mobile-terminal-overlay ==="
echo "Repo:    $REPO_ROOT"
echo "Service: $SERVICE_NAME (user)"
echo

cd "$REPO_ROOT"

# ── Step 1: Python sanity check ───────────────────────────────────────
# Cheap pre-flight that catches syntax errors before we touch anything
# else. import-only — does not exercise routes, doesn't need tmux.
echo "→ Python import check"
if ! "$PYTHON_VENV" -c "import mobile_terminal.server" 2>&1; then
    echo
    echo "✗ Python import failed — aborting before build/restart."
    echo "  Fix the error above, re-run."
    exit 1
fi
echo "  ok"
echo

# ── Step 2: Frontend build ────────────────────────────────────────────
if [[ $DO_BUILD -eq 1 ]]; then
    echo "→ Frontend build (sync-version + esbuild)"
    if ! npm run build; then
        echo
        echo "✗ Build failed — service left running on previous bundle."
        exit 1
    fi
    echo "  ok"
    echo
else
    echo "→ Build skipped (--skip-build)"
    echo
fi

# ── Step 3: Restart service ───────────────────────────────────────────
if [[ $DO_RESTART -eq 1 ]]; then
    echo "→ Restart $SERVICE_NAME (user)"
    if ! systemctl --user restart "$SERVICE_NAME"; then
        echo
        echo "✗ systemctl restart failed. Is the unit installed?"
        echo "    systemctl --user status $SERVICE_NAME"
        echo "    systemctl --user list-unit-files $SERVICE_NAME"
        exit 1
    fi
    # Give it a moment to bind the port before smoke-testing.
    sleep 2

    if ! systemctl --user is-active --quiet "$SERVICE_NAME"; then
        echo
        echo "✗ $SERVICE_NAME failed to stay running after restart."
        echo "    journalctl --user -u $SERVICE_NAME --since '1 min ago' | tail -30"
        exit 1
    fi
    echo "  ok"
    echo
else
    echo "→ Restart skipped (--no-restart)"
    echo
fi

# ── Step 4: Smoke tests ───────────────────────────────────────────────
# Only meaningful if we actually restarted (or if the service was
# already running on a prior version). Skip if the user explicitly
# opted out of restart and the service isn't running.
if [[ $DO_RESTART -eq 0 ]] && ! systemctl --user is-active --quiet "$SERVICE_NAME"; then
    echo "→ Smoke test skipped (service not running and --no-restart)"
    echo
else
    echo "→ Smoke test"

    health_code=$(curl -s -L -o /dev/null -w "%{http_code}" "$HEALTH_URL" || echo "000")
    if [[ "$health_code" != "200" ]]; then
        echo "  ✗ /health returned HTTP $health_code (expected 200)"
        echo "    journalctl --user -u $SERVICE_NAME --since '1 min ago' | tail -20"
        exit 1
    fi
    echo "  /health           → 200 ok"

    debug_code=$(curl -s -L -o /dev/null -w "%{http_code}" "$DEBUG_URL" || echo "000")
    if [[ "$debug_code" != "200" ]]; then
        echo "  ✗ /api/ws-debug returned HTTP $debug_code (expected 200)"
        exit 1
    fi
    echo "  /api/ws-debug     → 200 ok"
    echo
fi

# Print the version that just shipped so the operator can confirm it's
# what they expected (catches "forgot to bump version" too).
SHIPPED_VERSION=$(cat "$REPO_ROOT/scripts/version.txt" 2>/dev/null | tr -d '[:space:]' || echo "?")
echo "✓ Done — shipped v=$SHIPPED_VERSION."
echo "  Hard-reload the browser (Cmd/Ctrl+Shift+R) to drop client-cached JS."
