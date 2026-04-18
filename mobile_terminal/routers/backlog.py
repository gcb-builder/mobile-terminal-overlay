"""Routes for project-scoped backlog management."""
import logging
from dataclasses import asdict
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register backlog routes."""

    def _resolve_project(project: Optional[str], pane_id: Optional[str] = None) -> str:
        """Resolve project from param, pane cwd, or current repo path."""
        if project:
            return project
        # Try to resolve from pane's cwd
        if pane_id:
            try:
                import subprocess
                session = app.state.current_session
                from mobile_terminal.helpers import get_tmux_target
                tmux_t = get_tmux_target(session, pane_id)
                result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", tmux_t, "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=2,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
        repo_path = deps.get_current_repo_path()
        return str(repo_path) if repo_path else ""

    @app.get("/api/backlog/list")
    async def backlog_list(
        project: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """List all backlog items for a project."""
        project = _resolve_project(project, pane_id)
        if not project:
            return {"items": [], "project": ""}
        items = app.state.backlog_store.list_items(project)
        return {"items": [asdict(i) for i in items], "project": project}

    @app.post("/api/backlog/add")
    async def backlog_add(
        summary: str = Query(...),
        prompt: str = Query(...),
        source: str = Query("human"),
        origin: Optional[str] = Query(None),
        project: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Add a backlog item."""
        project = _resolve_project(project)
        if not project:
            return JSONResponse({"error": "No project context"}, status_code=400)
        # Resolve origin: explicit param > auto-detect from source
        if origin is None:
            origin = "api_report" if source == "agent" else "manual"
        item = app.state.backlog_store.add(project, summary, prompt, source, origin)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "backlog_update",
                    "action": "add",
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item)}

    @app.post("/api/backlog/update")
    async def backlog_update(
        id: str = Query(...),
        status: str = Query(...),
        queue_item_id: Optional[str] = Query(None),
        project: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Update a backlog item's status."""
        project = _resolve_project(project)
        item = app.state.backlog_store.update_status(
            project, id, status, queue_item_id
        )
        if item is None:
            return JSONResponse({"error": "Backlog item not found"}, status_code=404)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "backlog_update",
                    "action": "update",
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item)}

    @app.post("/api/backlog/remove")
    async def backlog_remove(
        id: str = Query(...),
        project: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Remove a backlog item."""
        project = _resolve_project(project)
        success = app.state.backlog_store.remove(project, id)

        if success and app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "backlog_update",
                    "action": "remove",
                    "item": {"id": id},
                })
            except Exception:
                pass

        if not success:
            return JSONResponse({"error": "Backlog item not found"}, status_code=404)
        return {"status": "ok"}

    # ── Candidate endpoints ────────────────────────────────────────────

    @app.get("/api/backlog/candidates")
    async def backlog_candidates(
        project: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """List current backlog candidates (ephemeral, in-memory)."""
        project = _resolve_project(project)
        cstore = app.state.candidate_store
        return {
            "candidates": [asdict(c) for c in cstore.list_candidates(project)],
            "project": project,
        }

    @app.post("/api/backlog/candidates/keep")
    async def candidate_keep(
        id: str = Query(...),
        project: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Promote a candidate to a durable backlog item."""
        project = _resolve_project(project)
        cstore = app.state.candidate_store
        candidate = cstore.remove(project, id)
        if not candidate:
            return JSONResponse({"error": "Candidate not found"}, status_code=404)

        store = app.state.backlog_store
        item = store.add(
            project, candidate.summary, candidate.prompt,
            source="agent", origin="jsonl_candidate",
        )

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "backlog_update",
                    "action": "add",
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item), "candidate_id": id}

    @app.post("/api/backlog/candidates/dismiss")
    async def candidate_dismiss(
        id: str = Query(...),
        project: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Dismiss a candidate (remembers hash to prevent re-detection)."""
        project = _resolve_project(project)
        cstore = app.state.candidate_store
        dismissed = cstore.dismiss(project, id)

        if dismissed and app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "backlog_candidate",
                    "action": "dismissed",
                    "candidate_id": id,
                })
            except Exception:
                pass

        if not dismissed:
            return JSONResponse({"error": "Candidate not found"}, status_code=404)
        return {"status": "ok"}
