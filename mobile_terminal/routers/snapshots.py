"""Routes for snapshot preview and audit log."""
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import get_project_id, get_tmux_target, run_subprocess

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register snapshot and audit routes."""

    @app.post("/api/rollback/preview/capture")
    async def capture_preview(
        label: str = Query("manual"),
        _auth=Depends(deps.verify_token),
    ):
        """Capture a snapshot of current session state."""

        session = app.state.current_session
        if not session:
            return JSONResponse({"error": "No active session"}, status_code=400)

        # Get log content (reuse /api/log logic)
        try:
            repo_path = deps.get_current_repo_path()
            if repo_path:
                project_id = get_project_id(repo_path, strip_leading=True)
                claude_dir = Path.home() / ".claude" / "projects" / project_id
                jsonl_files = sorted(claude_dir.glob("*.jsonl"),
                                   key=lambda f: f.stat().st_mtime, reverse=True)
                log_content = jsonl_files[0].read_text(errors="replace") if jsonl_files else ""
            else:
                log_content = ""
        except Exception as e:
            log_content = f"[Error reading log: {e}]"

        # Capture terminal (last 50 lines)
        try:
            # Use active target pane if set, otherwise fall back to session default
            target = get_tmux_target(session, app.state.active_target)
            result = await run_subprocess(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", "-50"],
                capture_output=True, text=True, timeout=5
            )
            terminal_text = result.stdout if result.returncode == 0 else ""
        except Exception:
            terminal_text = ""

        # Get queue state
        queue_state = [asdict(item) for item in app.state.command_queue.list_items(session)]

        # Capture snapshot
        logger.info(f"Capturing snapshot: session={session}, label={label}, log_len={len(log_content)}, term_len={len(terminal_text)}")
        snapshot = app.state.snapshot_buffer.capture(
            session, label, log_content, terminal_text, queue_state
        )

        if snapshot:
            logger.info(f"Snapshot created: {snapshot['id']}")
            app.state.audit_log.log("snapshot_capture", {"snap_id": snapshot["id"], "label": label})
            return {"success": True, "snapshot": {"id": snapshot["id"], "label": label}}
        logger.info("Snapshot skipped: content unchanged")
        return {"success": True, "snapshot": None, "reason": "unchanged"}

    @app.get("/api/rollback/previews")
    async def list_previews(
        limit: int = Query(50),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """List available snapshots, optionally filtered by pane_id."""

        session = app.state.current_session
        target = pane_id or app.state.active_target
        buf = app.state.snapshot_buffer

        # Try target-scoped key first, then fall back to session-only
        snap_key = f"{session}:{target}" if target else session
        snapshots = buf.list_snapshots(snap_key, limit)
        if not snapshots and target:
            # Fall back to session-only snapshots (legacy)
            snapshots = buf.list_snapshots(session, limit)

        logger.info(f"List snapshots: key={snap_key}, count={len(snapshots)}")
        return {"session": session, "pane_id": target, "snapshots": snapshots}

    @app.get("/api/rollback/preview/{snap_id}")
    async def get_preview(
        snap_id: str,
        _auth=Depends(deps.verify_token),
    ):
        """Get full snapshot data. Populates heavy fields on demand."""

        session = app.state.current_session
        target = app.state.active_target
        buf = app.state.snapshot_buffer

        # Search in target-scoped key first, then session
        snap_key = f"{session}:{target}" if target else session
        snapshot = buf.get_snapshot(snap_key, snap_id)
        if not snapshot and target:
            snapshot = buf.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        # Populate heavy fields on demand if they're empty (lazy loading)
        if not snapshot.get("terminal_text") and snapshot.get("pane_id"):
            try:
                tmux_t = get_tmux_target(session, snapshot["pane_id"])
                cap = await run_subprocess(
                    ["tmux", "capture-pane", "-p", "-S", "-100", "-t", tmux_t],
                    capture_output=True, text=True, timeout=3,
                )
                if cap.returncode == 0:
                    snapshot["terminal_text"] = cap.stdout or ""
            except Exception:
                pass

        if not snapshot.get("log_entries") and snapshot.get("log_path") and snapshot.get("log_offset"):
            try:
                lp = Path(snapshot["log_path"])
                if lp.exists():
                    # Read last 4KB before the offset for context
                    offset = snapshot["log_offset"]
                    read_start = max(0, offset - 4096)
                    with open(lp, 'rb') as f:
                        f.seek(read_start)
                        data = f.read(offset - read_start)
                    snapshot["log_entries"] = data.decode('utf-8', errors='replace')
            except Exception:
                pass

        return snapshot

    @app.post("/api/rollback/preview/{snap_id}/annotate")
    async def annotate_snapshot(
        snap_id: str,
        request: Request,
        _auth=Depends(deps.verify_token),
    ):
        """Add a note or image_path to a snapshot."""

        body = await request.json()
        note = body.get("note", "")
        image_path = body.get("image_path")

        # Cap note at 500 chars
        if note and len(note) > 500:
            note = note[:500]

        session = app.state.current_session
        target = app.state.active_target
        buf = app.state.snapshot_buffer

        # Search in target-scoped key first, then session
        snap_key = f"{session}:{target}" if target else session
        snapshot = None
        with buf._lock:
            snaps = buf._snapshots.get(snap_key, {})
            if snap_id in snaps:
                snapshot = snaps[snap_id]
            elif target:
                snaps = buf._snapshots.get(session, {})
                if snap_id in snaps:
                    snapshot = snaps[snap_id]

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        if note is not None:
            snapshot["note"] = note
        if image_path is not None:
            snapshot["image_path"] = image_path

        app.state.audit_log.log("snapshot_annotate", {"snap_id": snap_id, "note": note[:50] if note else ""})
        return {"success": True, "snap_id": snap_id}

    @app.post("/api/rollback/preview/select")
    async def select_preview(
        snap_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Enter or exit preview mode (snap_id=null exits)."""

        session = app.state.current_session

        if snap_id:
            # Verify snapshot exists
            snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)
            if not snapshot:
                return JSONResponse({"error": "Snapshot not found"}, status_code=404)
            app.state.audit_log.log("preview_enter", {"snap_id": snap_id})
        else:
            app.state.audit_log.log("preview_exit", {})

        return {"success": True, "preview_mode": snap_id is not None, "snap_id": snap_id}

    @app.post("/api/rollback/preview/{snap_id}/pin")
    async def pin_snapshot(
        snap_id: str,
        pinned: bool = Query(True),
        _auth=Depends(deps.verify_token),
    ):
        """Pin or unpin a snapshot to prevent eviction."""

        session = app.state.current_session
        success = app.state.snapshot_buffer.pin_snapshot(session, snap_id, pinned)

        if not success:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        app.state.audit_log.log("snapshot_pin", {"snap_id": snap_id, "pinned": pinned})
        return {"success": True, "snap_id": snap_id, "pinned": pinned}

    @app.get("/api/rollback/preview/{snap_id}/export")
    async def export_snapshot(
        snap_id: str,
        _auth=Depends(deps.verify_token),
    ):
        """Export snapshot as JSON file."""

        session = app.state.current_session
        snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        app.state.audit_log.log("snapshot_export", {"snap_id": snap_id})

        from starlette.responses import Response
        return Response(
            json.dumps(snapshot, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={snap_id}.json"}
        )

    @app.get("/api/rollback/preview/diff")
    async def diff_snapshots(
        snap_a: str = Query(...),
        snap_b: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """Compare two snapshots."""

        session = app.state.current_session
        a = app.state.snapshot_buffer.get_snapshot(session, snap_a)
        b = app.state.snapshot_buffer.get_snapshot(session, snap_b)

        if not a:
            return JSONResponse({"error": f"Snapshot {snap_a} not found"}, status_code=404)
        if not b:
            return JSONResponse({"error": f"Snapshot {snap_b} not found"}, status_code=404)

        return {
            "a": {"id": snap_a, "timestamp": a["timestamp"], "label": a["label"]},
            "b": {"id": snap_b, "timestamp": b["timestamp"], "label": b["label"]},
            "log_changed": a["log_hash"] != b["log_hash"],
            "terminal_changed": a["terminal_text"] != b["terminal_text"],
            "queue_changed": a["queue_state"] != b["queue_state"],
        }

    @app.post("/api/rollback/preview/clear")
    async def clear_previews(
        _auth=Depends(deps.verify_token),
    ):
        """Clear all snapshots for current session."""

        session = app.state.current_session
        count = app.state.snapshot_buffer.clear(session)
        app.state.audit_log.log("snapshots_cleared", {"count": count})
        logger.info(f"Cleared {count} snapshots for session {session}")
        return {"success": True, "cleared": count}

    @app.get("/api/rollback/audit")
    async def get_audit_log(
        limit: int = Query(100),
        _auth=Depends(deps.verify_token),
    ):
        """Get recent audit log entries."""

        entries = app.state.audit_log.get_entries(limit)
        return {"entries": entries}

    @app.post("/api/rollback/audit/log")
    async def log_audit_action(
        action: str = Query(...),
        details: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Log an audit action from client."""

        detail_dict = {}
        if details:
            try:
                detail_dict = json.loads(details)
            except:
                detail_dict = {"raw": details}

        app.state.audit_log.log(action, detail_dict)
        return {"success": True}
