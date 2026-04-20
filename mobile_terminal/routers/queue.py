"""Routes for command queue management."""
import logging
from dataclasses import asdict
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register queue routes."""

    @app.post("/api/queue/enqueue")
    async def queue_enqueue(
        text: str = Query(...),
        session: str = Query(...),
        policy: str = Query("auto"),
        id: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
        backlog_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """
        Add command to the deferred queue.

        Policy:
        - "auto": server determines safe/unsafe based on text
        - "safe": force auto-send when ready
        - "unsafe": always require manual confirmation

        If `id` is provided and already exists, returns the existing item
        without creating a duplicate (idempotency).
        """

        item, is_new = app.state.command_queue.enqueue(session, text, policy, item_id=id, pane_id=pane_id, backlog_id=backlog_id)

        # Notify connected clients only for new items. Stamp session+pane
        # so views for other panes can ignore the message — without this,
        # any connected client picks up every queue change in every pane
        # and writes them into the currently-visible queue, causing the
        # cross-pane bleed users have been seeing.
        if is_new and app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_update",
                    "action": "add",
                    "session": session,
                    "pane_id": pane_id,
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item), "is_new": is_new}

    @app.get("/api/queue/list")
    async def queue_list(
        session: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """List all queued items for a session+pane."""

        items = app.state.command_queue.list_items(session, pane_id)
        paused = app.state.command_queue.is_paused(session, pane_id)
        return {
            "items": [asdict(i) for i in items],
            "paused": paused,
            "session": session,
        }

    @app.post("/api/queue/remove")
    async def queue_remove(
        session: str = Query(...),
        item_id: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Remove an item from the queue."""

        success = app.state.command_queue.dequeue(session, item_id, pane_id)

        if success and app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_update",
                    "action": "remove",
                    "session": session,
                    "pane_id": pane_id,
                    "item": {"id": item_id},
                })
            except Exception:
                pass

        if not success:
            return JSONResponse({"error": "Queue item not found"}, status_code=404)
        return {"status": "ok"}

    @app.post("/api/queue/auto_eligible")
    async def queue_auto_eligible(
        session: str = Query(...),
        item_id: str = Query(...),
        value: bool = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Toggle the auto-drain opt-in flag on a queue item. Items
        default to auto_eligible=False so the processor never fires
        until the user explicitly ⚡-flags them in the UI."""
        item = app.state.command_queue.set_auto_eligible(session, item_id, value, pane_id)
        if item is None:
            return JSONResponse({"error": "Queue item not found or not queued"}, status_code=404)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_update",
                    "action": "update",
                    "session": session,
                    "pane_id": pane_id,
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item)}

    @app.post("/api/queue/mark_sent")
    async def queue_mark_sent(
        session: str = Query(...),
        item_id: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Mark a queued item as sent — for client-driven manual sends
        where the text was already delivered to the PTY by the client
        (e.g. row Send button or Run while paused). Without this the
        server-side queue still treats the item as queued."""
        item = app.state.command_queue.mark_sent(session, item_id, pane_id)
        if item is None:
            return JSONResponse({"error": "Queue item not found"}, status_code=404)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_sent",
                    "id": item.id,
                    "sent_at": item.sent_at,
                    "backlog_id": item.backlog_id,
                    "session": session,
                    "pane_id": pane_id,
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item)}

    @app.post("/api/queue/reorder")
    async def queue_reorder(
        session: str = Query(...),
        item_id: str = Query(...),
        new_index: int = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Reorder an item in the queue."""

        success = app.state.command_queue.reorder(session, item_id, new_index, pane_id)
        if not success:
            return JSONResponse({"error": "Queue item not found"}, status_code=404)
        return {"status": "ok"}

    @app.post("/api/queue/pause")
    async def queue_pause(
        session: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Pause queue processing for a session+pane."""

        app.state.command_queue.pause(session, pane_id)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_state",
                    "session": session,
                    "pane_id": pane_id,
                    "paused": True,
                    "count": len(app.state.command_queue.list_items(session, pane_id)),
                })
            except Exception:
                pass

        return {"status": "ok", "paused": True}

    @app.post("/api/queue/resume")
    async def queue_resume(
        session: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Resume queue processing for a session+pane."""

        app.state.command_queue.resume(session, pane_id)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_state",
                    "session": session,
                    "pane_id": pane_id,
                    "paused": False,
                    "count": len(app.state.command_queue.list_items(session, pane_id)),
                })
            except Exception:
                pass

        return {"status": "ok", "paused": False}

    @app.post("/api/queue/flush")
    async def queue_flush(
        session: str = Query(...),
        confirm: bool = Query(False),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Clear all queued items. Requires confirm=true."""

        if not confirm:
            items = app.state.command_queue.list_items(session, pane_id)
            return {"status": "confirm_required", "count": len(items)}

        count = app.state.command_queue.flush(session, pane_id)

        if app.state.active_client:
            try:
                await app.state.active_client.send_json({
                    "type": "queue_state",
                    "session": session,
                    "pane_id": pane_id,
                    "paused": False,
                    "count": 0,
                })
            except Exception:
                pass

        return {"status": "ok", "cleared": count}

    @app.post("/api/queue/send-next")
    async def queue_send_next(
        session: str = Query(...),
        item_id: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """
        Manually send the next unsafe item (or specific item).
        Bypasses policy check for one item.
        """

        item = await app.state.command_queue.send_next_unsafe(session, item_id, pane_id)

        if item:
            return {"status": "ok", "item": asdict(item)}
        return JSONResponse(
            {"error": "No unsafe items in queue"},
            status_code=404,
        )
