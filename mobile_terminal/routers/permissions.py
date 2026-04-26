"""Routes for permission policy management."""
import logging
from dataclasses import asdict
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register permission policy routes."""

    @app.get("/api/permissions/rules")
    async def permissions_rules(_auth=Depends(deps.verify_token)):
        """List all rules and current mode."""
        policy = app.state.permission_policy
        rules = policy.list_rules()
        return {
            "mode": policy.mode,
            "rules": [asdict(r) for r in rules],
            "repo": str(deps.get_current_repo_path() or ""),
        }

    @app.post("/api/permissions/rules")
    async def permissions_add_rule(
        tool: str = Query(...),
        matcher_type: str = Query(...),
        matcher: str = Query(""),
        scope: str = Query(...),
        scope_value: str = Query(""),
        action: str = Query("allow"),
        created_from: str = Query("banner"),
        note: str = Query(""),
        bypass_hard_guard: bool = Query(False),
        _auth=Depends(deps.verify_token),
    ):
        """Create a new permission rule."""
        policy = app.state.permission_policy
        if scope not in ("global", "repo", "session"):
            return JSONResponse({"error": "Invalid scope"}, status_code=400)
        if action not in ("allow", "prompt", "deny"):
            return JSONResponse({"error": "Invalid action"}, status_code=400)
        if matcher_type not in ("command", "path", "tool_only"):
            return JSONResponse({"error": "Invalid matcher_type"}, status_code=400)
        rule = policy.add_rule(
            tool=tool,
            matcher_type=matcher_type,
            matcher=matcher,
            scope=scope,
            scope_value=scope_value or None,
            action=action,
            created_from=created_from,
            note=note or None,
            bypass_hard_guard=bypass_hard_guard,
        )
        return {"status": "ok", "rule": asdict(rule)}

    @app.delete("/api/permissions/rules")
    async def permissions_delete_rule(
        id: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """Delete a permission rule by ID."""
        policy = app.state.permission_policy
        if id.startswith("default_"):
            return JSONResponse({"error": "Cannot delete default rules"}, status_code=400)
        removed = policy.remove_rule(id)
        if not removed:
            return JSONResponse({"error": "Rule not found"}, status_code=404)
        return {"status": "ok"}

    @app.post("/api/permissions/mode")
    async def permissions_set_mode(
        mode: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """Set the permission policy mode."""
        policy = app.state.permission_policy
        try:
            policy.set_mode(mode)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"status": "ok", "mode": policy.mode}

    @app.get("/api/permissions/audit")
    async def permissions_audit(
        limit: int = Query(50),
        _auth=Depends(deps.verify_token),
    ):
        """Read recent audit log entries."""
        policy = app.state.permission_policy
        entries = policy.read_audit(limit=limit)
        return {"entries": entries}

    @app.post("/api/permissions/test")
    async def permissions_test(
        tool: str = Query("Bash"),
        target: str = Query("pytest tests/ -q"),
        source_pane: Optional[str] = Query(None, description="Override source_pane (defaults to active_target). Use to validate cross-pane routing."),
        _auth=Depends(deps.verify_token),
    ):
        """Return a fake permission_request payload for the frontend to display.

        The frontend shows the enhanced banner. Pass ?source_pane=X:Y
        to validate that Allow/Deny routes the y/n to that specific
        pane (not whichever pane is globally active).
        """
        import uuid
        repo = str(deps.get_current_repo_path() or "")
        from mobile_terminal.permission_policy import classify_risk
        perm = {
            "id": str(uuid.uuid4()),
            "tool": tool,
            "target": target,
            "repo": repo,
            "risk": classify_risk(tool, target),
            "source_pane": source_pane or app.state.active_target,
        }
        # Store for polling clients and broadcast to all connected sinks.
        app.state._test_permission = perm
        from mobile_terminal.transport import broadcast_typed
        await broadcast_typed(app, "permission_request", perm, level="urgent")
        return {"status": "ok", "perm": perm}

    @app.get("/api/permissions/test")
    async def permissions_test_poll(_auth=Depends(deps.verify_token)):
        """Poll for pending test permission (for HTTP-polling clients)."""
        perm = getattr(app.state, '_test_permission', None)
        if perm:
            app.state._test_permission = None
            return {"perm": perm}
        return {"perm": None}

    @app.get("/api/permissions/waiting")
    async def permissions_waiting(
        pane: str = "",
        _auth=Depends(deps.verify_token),
    ):
        """Return whether Claude has a permission prompt pending for the
        given tmux pane (e.g. "1:0", "2:0").

        Resolves pane → cwd via tmux display-message, then reads
        ~/.claude/sessions/{pid}.json and reports status="waiting" +
        waitingFor="approve <Tool>" for that cwd. Used by the client
        scraper to skip building a banner when no real prompt is live
        — eliminates false positives where the scraper detects
        prompt-shape text in scrollback OR in MTO chat content quoting
        prompt strings.

        Response: {"waiting": bool, "tool": str|null, "sessionId": str|null}
        """
        import subprocess
        from mobile_terminal.helpers import get_tmux_target
        from mobile_terminal.permission_daemon import _load_waiting_sessions

        if not pane:
            return {"waiting": False, "tool": None, "sessionId": None}

        # Resolve pane → cwd
        try:
            session = app.state.current_session
            tmux_t = get_tmux_target(session, pane)
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", tmux_t, "#{pane_current_path}"],
                capture_output=True, text=True, timeout=2,
            )
            cwd = result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            cwd = ""

        if not cwd:
            return {"waiting": False, "tool": None, "sessionId": None}

        try:
            sessions = _load_waiting_sessions()
        except Exception:
            return {"waiting": False, "tool": None, "sessionId": None}
        if cwd in sessions:
            entry = sessions[cwd]
            return {
                "waiting": True,
                "tool": entry.get("tool"),
                "sessionId": entry.get("sessionId"),
            }
        return {"waiting": False, "tool": None, "sessionId": None}

    # Phase 4 (2026-04-25): /api/permissions/decide endpoint removed.
    # The client-side scraper (extractPermissionPrompt in terminal.js) used
    # to POST visual detections here so the server could fire y/n. That path
    # had no JSONL correlation — a stale "Read" selector left in scrollback
    # would re-trigger when the agent posted unrelated chat, causing a
    # spurious "1" injection. Auto-fire authority now lives exclusively in
    # the server-side daemon (permission_daemon.py) and scanner (push.py),
    # both of which correlate against the JSONL unresolved-tool_use state
    # and refuse to fire when no real perm is pending. The client scraper
    # still runs to populate the tap-to-approve banner UI, but no longer
    # calls a server endpoint to fire on its behalf.
