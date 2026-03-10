"""Routes for scratch storage (content-addressable tool output store)."""

import logging
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import get_project_id

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register scratch storage routes."""

    def _resolve_project_id(override: Optional[str] = None) -> Optional[str]:
        if override:
            return override
        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return None
        return get_project_id(repo_path, strip_leading=True)

    @app.get("/api/scratch/list")
    async def scratch_list(
        limit: int = Query(100),
        tool_name: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        project_id = _resolve_project_id()
        if not project_id:
            return JSONResponse({"error": "No project context"}, status_code=400)
        entries = app.state.scratch_store.list_entries(project_id, limit, tool_name)
        return {"entries": entries, "project_id": project_id, "count": len(entries)}

    @app.get("/api/scratch/stats")
    async def scratch_stats(
        _auth=Depends(deps.verify_token),
    ):
        project_id = _resolve_project_id()
        if not project_id:
            return JSONResponse({"error": "No project context"}, status_code=400)
        return app.state.scratch_store.get_stats(project_id)

    @app.get("/api/scratch/{content_hash}/summary")
    async def scratch_get_summary(
        content_hash: str,
        _auth=Depends(deps.verify_token),
    ):
        project_id = _resolve_project_id()
        if not project_id:
            return JSONResponse({"error": "No project context"}, status_code=400)
        entry = app.state.scratch_store.get_summary(content_hash, project_id)
        if entry is None:
            return JSONResponse(
                {"error": f"Entry not found: {content_hash}"}, status_code=404
            )
        return entry

    @app.get("/api/scratch/{content_hash}")
    async def scratch_get(
        content_hash: str,
        _auth=Depends(deps.verify_token),
    ):
        project_id = _resolve_project_id()
        if not project_id:
            return JSONResponse({"error": "No project context"}, status_code=400)
        entry = app.state.scratch_store.get(content_hash, project_id)
        if entry is None:
            return JSONResponse(
                {"error": f"Entry not found: {content_hash}"}, status_code=404
            )
        return entry

    @app.post("/api/scratch/store")
    async def scratch_store(
        request: Request,
        project_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        pid = _resolve_project_id(project_id)
        if not pid:
            return JSONResponse({"error": "No project context"}, status_code=400)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        content = body.get("content")
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        result = app.state.scratch_store.store(
            content=content,
            tool_name=body.get("tool_name", "unknown"),
            target=body.get("target", ""),
            summary=body.get("summary", ""),
            session_id=body.get("session_id", ""),
            pane_id=body.get("pane_id", ""),
            project_id=pid,
        )

        if result is None:
            return JSONResponse(
                {"error": "Failed to store (disk error)"}, status_code=507
            )
        return {"stored": True, **result}

    @app.delete("/api/scratch/{content_hash}")
    async def scratch_delete(
        content_hash: str,
        _auth=Depends(deps.verify_token),
    ):
        project_id = _resolve_project_id()
        if not project_id:
            return JSONResponse({"error": "No project context"}, status_code=400)
        success = app.state.scratch_store.delete(content_hash, project_id)
        if not success:
            return JSONResponse(
                {"error": f"Entry not found: {content_hash}"}, status_code=404
            )
        return {"deleted": True, "content_hash": content_hash}
