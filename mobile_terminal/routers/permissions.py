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

    # ── Decide endpoint ─────────────────────────────────────────────────
    # Bridge between the client-side terminal scraper (extractPermissionPrompt)
    # and the server-side policy engine. The client detects a permission
    # prompt visually (it's better at parsing Claude's TUI rendering than the
    # server's regex) and POSTs here. The server runs policy.evaluate, fires
    # y/n via runtime if allow/deny, writes audit, and returns the decision.
    # On needs_human the client falls back to showing its own banner.
    #
    # Has SIDE EFFECTS (may inject keystrokes). Not read-only — name reflects
    # that. Dedup window prevents the client scraper and server scanner from
    # double-firing on the same perm.

    _decide_dedup: dict = {}  # f"{pane}:{tool}:{hash(target)}" -> ts
    DECIDE_DEDUP_TTL = 30.0

    @app.post("/api/permissions/decide")
    async def permissions_decide(
        request: Request,
        _auth=Depends(deps.verify_token),
    ):
        """Run policy on a client-detected permission and fire y/n if allowed."""
        import time as _time
        from mobile_terminal.permission_policy import normalize_request

        body = await request.json()
        tool = (body.get("tool") or "").strip()
        target = (body.get("target") or "").strip()
        repo = (body.get("repo") or "").strip()
        source_pane = (body.get("source_pane") or "").strip()

        if not tool:
            return JSONResponse({"error": "tool required"}, status_code=400)
        if not source_pane:
            return JSONResponse({"error": "source_pane required"}, status_code=400)

        # Dedup: same {pane,tool,target} within TTL → already handled.
        # Hash the full target so two different `Bash: ls` calls in the same
        # pane don't collide with each other across time but a re-detect of
        # the same in-flight perm does.
        dedup_key = f"{source_pane}:{tool}:{hash(target)}"
        now = _time.time()
        # Sweep stale entries occasionally to keep the dict bounded
        if len(_decide_dedup) > 100:
            for k, ts in list(_decide_dedup.items()):
                if now - ts > DECIDE_DEDUP_TTL:
                    del _decide_dedup[k]
        last = _decide_dedup.get(dedup_key, 0)
        if now - last < DECIDE_DEDUP_TTL:
            return {"decision": "already_handled", "reason": "dedup", "rule_id": None}

        # Build PermissionRequest and evaluate
        perm = {"tool": tool, "target": target}
        repo_path = repo or str(deps.get_current_repo_path() or "")
        req = normalize_request(perm, repo_path)
        policy = app.state.permission_policy
        decision = policy.evaluate(req)
        policy.audit(req, decision)

        # Side-effect: fire y/n if allow/deny. Dedup AFTER firing so a
        # transient error doesn't poison the key.
        runtime = app.state.runtime
        session = app.state.current_session

        async def _suppress_other_paths(verb: str):
            """Tell the rest of the system this perm was just handled.

            - Stamp the scanner's cooldown dict so the 3s scan loop
              doesn't fire y/n again for the same pane within ~5s.
            - Push permission_auto to the active client so its scraper
              suppresses the banner for 5s (_permAutoApprovedAt path).
            Without this, the user sees a second banner appear seconds
            after /decide already fired — same prompt still in scrollback,
            other detectors haven't been told it's resolved.
            """
            try:
                if not hasattr(app.state, "permission_scanner_cooldown"):
                    app.state.permission_scanner_cooldown = {}
                app.state.permission_scanner_cooldown[source_pane] = now
            except Exception as e:
                logger.debug(f"[decide] scanner-cooldown stamp failed: {e}")
            try:
                sink = app.state.active_client
                if sink is not None:
                    await deps.send_typed(sink, "permission_auto", {
                        "decision": verb,
                        "tool": tool,
                        "target": target[:80],
                        "reason": decision.reason,
                        "pane": source_pane,
                    }, level="info")
            except Exception as e:
                logger.debug(f"[decide] permission_auto emit failed: {e}")

        if decision.action == "allow":
            from mobile_terminal.helpers import get_tmux_target
            tmux_t = get_tmux_target(session, source_pane) if session else source_pane
            try:
                await runtime.send_keys(tmux_t, "y", literal=True)
                await runtime.send_keys(tmux_t, "Enter")
                _decide_dedup[dedup_key] = now
                logger.info(f"[decide] auto-allow {tool} in {source_pane}: {decision.reason}")
                await _suppress_other_paths("allow")
            except Exception as e:
                logger.warning(f"[decide] send_keys failed for {source_pane}: {e}")
                return JSONResponse({"error": "send failed"}, status_code=500)
        elif decision.action == "deny":
            from mobile_terminal.helpers import get_tmux_target
            tmux_t = get_tmux_target(session, source_pane) if session else source_pane
            try:
                await runtime.send_keys(tmux_t, "n", literal=True)
                await runtime.send_keys(tmux_t, "Enter")
                _decide_dedup[dedup_key] = now
                logger.info(f"[decide] auto-deny {tool} in {source_pane}: {decision.reason}")
                await _suppress_other_paths("deny")
            except Exception as e:
                logger.warning(f"[decide] send_keys failed for {source_pane}: {e}")
                return JSONResponse({"error": "send failed"}, status_code=500)
        # needs_human: do NOT dedup — user may dismiss banner and want a
        # second chance to be re-prompted on the same pending perm.

        return {
            "decision": decision.action,  # "allow" | "deny" | "prompt"
            "reason": decision.reason,
            "rule_id": decision.rule_id,
        }
