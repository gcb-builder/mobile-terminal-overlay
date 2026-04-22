"""Routes for permission policy management."""
import logging
from dataclasses import asdict
from typing import Optional

from fastapi import Depends, FastAPI, Query
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
        # Store for polling clients and try direct send
        app.state._test_permission = perm
        sink = app.state.active_client
        if sink is not None:
            await deps.send_typed(sink, "permission_request", perm, level="urgent")
        return {"status": "ok", "perm": perm}

    @app.get("/api/permissions/test")
    async def permissions_test_poll(_auth=Depends(deps.verify_token)):
        """Poll for pending test permission (for HTTP-polling clients)."""
        perm = getattr(app.state, '_test_permission', None)
        if perm:
            app.state._test_permission = None
            return {"perm": perm}
        return {"perm": None}
