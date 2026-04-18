"""Routes for terminal I/O over Server-Sent Events (SSE).

Provides the same terminal streaming as the WebSocket handler but over
HTTP SSE.  Input flows via separate POST endpoints instead of the
bidirectional WebSocket channel.
"""
import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import time
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from mobile_terminal.helpers import (
    strip_ansi,
    find_utf8_boundary,
    get_tmux_target,
    send_text_to_pane,
)
from mobile_terminal import terminal_session as _tsess

logger = logging.getLogger(__name__)


class SSESink:
    """ClientSink backed by an asyncio.Queue for SSE streaming."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._connected: bool = True
        self.client_mode: str = "tail"
        self._last_overflow_log: float = 0.0

    # ---- ClientSink protocol ------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def transport_type(self) -> str:
        return "sse"

    async def send_json(self, data: dict) -> None:
        payload = json.dumps(data, separators=(",", ":"))
        await self._enqueue(f"event: message\ndata: {payload}\n\n")

    async def send_bytes(self, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        await self._enqueue(f"event: binary\ndata: {encoded}\n\n")

    async def send_text(self, text: str) -> None:
        # Escape embedded newlines so each logical line stays in one SSE data frame
        escaped = text.replace("\n", "\\n").replace("\r", "\\r")
        await self._enqueue(f"event: text\ndata: {escaped}\n\n")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self._connected:
            return
        # Send close reason as final event before disconnecting
        close_msg = json.dumps({"type": "close", "code": code, "reason": reason})
        try:
            self._queue.put_nowait(f"event: message\ndata: {close_msg}\n\n")
        except asyncio.QueueFull:
            pass
        self._connected = False
        # Push sentinel to unblock the generator
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    # ---- internals -----------------------------------------------------------

    async def _enqueue(self, frame: str) -> None:
        if not self._connected:
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop oldest item to make room
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass
            now = time.time()
            if now - self._last_overflow_log > 10.0:
                self._last_overflow_log = now
                logger.warning("SSESink queue overflow — dropped oldest item")


def register(app: FastAPI, deps):
    """Register SSE terminal streaming and POST input routes."""

    # ------------------------------------------------------------------
    # GET /api/terminal/stream  — SSE event stream
    # ------------------------------------------------------------------

    @app.get("/api/terminal/stream")
    async def terminal_stream(
        request: Request,
        _auth=Depends(deps.verify_token),
    ):
        sink = SSESink()

        # Close any existing connection (single-client mode, cross-transport).
        # Bounded with a short timeout: if the previous client is on a
        # dead network (mobile WiFi→cellular switch, etc.) the close
        # frame would otherwise wait at the TCP layer until the OS
        # timeout (30-120s), blocking this new connection's setup and
        # forcing the client into a reconnect-storm. 1s is plenty for a
        # healthy close; anything longer is a dead socket and we just
        # abandon it — the OLD handler's gather will tear it down once
        # its tasks notice active_client changed.
        if app.state.active_client is not None:
            try:
                await asyncio.wait_for(
                    app.state.active_client.close(code=4002),
                    timeout=1.0,
                )
                logger.info("Closed previous client connection (cross-transport)")
            except asyncio.TimeoutError:
                logger.warning("Previous client close timed out — abandoning (likely dead socket)")
            except Exception:
                pass
        if app.state.read_task is not None:
            app.state.read_task.cancel()
            app.state.read_task = None

        app.state.active_client = sink

        # Spawn tmux if not already running
        runtime = app.state.runtime
        if not runtime.has_fd:
            try:
                session_name = app.state.current_session
                runtime.spawn(session_name)
                logger.info(f"Spawned tmux session: {session_name}")
            except Exception as e:
                logger.error(f"Failed to spawn tmux: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        output_buffer = app.state.output_buffer

        # Send hello as first event
        hello_msg = {
            "type": "hello",
            "session": app.state.current_session,
            "pid": runtime.child_pid,
            "started_at": int(time.time()),
        }
        await sink.send_json(hello_msg)
        logger.info(f"SSE hello sent: {hello_msg}")

        # Send capture-pane snapshot instead of clearing screen —
        # client keeps last frame visible during reconnect, snapshot refreshes it
        try:
            session = app.state.current_session
            target = app.state.active_target
            tmux_t = get_tmux_target(session, target) if target else session
            snapshot = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["tmux", "capture-pane", "-p", "-e", "-t", tmux_t],
                    capture_output=True, text=True, timeout=2,
                ).stdout or ""
            )
            if snapshot:
                await sink.send_text("\x1b[2J\x1b[H" + snapshot)
        except Exception as e:
            # Fallback to plain clear if capture fails
            await sink.send_text("\x1b[2J\x1b[H")
            logger.debug(f"capture-pane on SSE connect failed: {e}")

        # Per-connection state lives in TerminalSessionState — same struct
        # the WS handler uses, shared via mobile_terminal.terminal_session.
        # See PR4 for the WS/SSE extraction.
        state = _tsess.TerminalSessionState(
            sink=sink,
            runtime=runtime,
            output_buffer=output_buffer,
            last_target_epoch=app.state.target_epoch,
        )

        async def server_keepalive():
            """Send SSE keepalive comments every 25s.

            Transport-specific: SSE is unidirectional, so this just emits
            an SSE comment line to keep the TCP connection from being
            closed by middleboxes. The WS equivalent uses bidirectional
            ping/pong with ghost detection — they are intentionally
            different shapes per transport.
            """
            KEEPALIVE_INTERVAL = 25
            while app.state.active_client is sink and not state.connection_closed:
                try:
                    await asyncio.sleep(KEEPALIVE_INTERVAL)
                    if app.state.active_client is sink and not state.connection_closed:
                        await sink._enqueue(": keepalive\n\n")
                except Exception:
                    break

        # read_from_terminal, tail_sender, and desktop_activity_monitor
        # live in mobile_terminal.terminal_session — see PR4 for the
        # WS/SSE extraction rationale.

        # ---- SSE generator ---------------------------------------------------

        async def event_generator():
            # Spawn background tasks
            read_task = asyncio.create_task(_tsess.read_from_terminal(state, app))
            app.state.read_task = read_task
            keepalive_task = asyncio.create_task(server_keepalive())
            tail_task = asyncio.create_task(_tsess.tail_sender(state, app, deps))
            desktop_task = asyncio.create_task(_tsess.desktop_activity_monitor(state, app, deps))

            try:
                while sink.is_connected:
                    try:
                        frame = await asyncio.wait_for(sink._queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        # Safety net — should not happen with keepalive, but
                        # yield an SSE comment to keep connection alive
                        yield ": timeout-keepalive\n\n"
                        continue

                    if frame is None:
                        # Sentinel — stream is done
                        break

                    yield frame

                    # Check if client disconnected
                    if await request.is_disconnected():
                        break
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"SSE generator error: {e}")
            finally:
                state.connection_closed = True
                read_task.cancel()
                keepalive_task.cancel()
                tail_task.cancel()
                desktop_task.cancel()
                if app.state.active_client is sink:
                    app.state.active_client = None
                    app.state.read_task = None

                if pty_died:
                    logger.warning("SSE stream ended — PTY died")
                    runtime.close_fd()

                sink._connected = False
                logger.info("SSE connection closed")

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------
    # POST endpoints for SSE clients
    # ------------------------------------------------------------------

    @app.post("/api/terminal/input")
    async def terminal_input(
        request: Request,
        _auth=Depends(deps.verify_token),
    ):
        """Raw bytes body written directly to PTY."""
        body = await request.body()
        if not body:
            return JSONResponse({"error": "Empty body"}, status_code=400)
        runtime = app.state.runtime
        if not runtime.has_fd:
            return JSONResponse({"error": "No active terminal"}, status_code=400)
        runtime.pty_write(body)
        app.state.last_ws_input_time = time.time()
        return {"ok": True}

    @app.post("/api/terminal/text")
    async def terminal_text(
        request: Request,
        _auth=Depends(deps.verify_token),
    ):
        """JSON {text, enter} — send text via tmux send-keys."""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        text_data = data.get("text", "")
        send_enter = data.get("enter", False)

        runtime = app.state.runtime
        session = app.state.current_session
        target = data.get("pane_id") or app.state.active_target
        tmux_t = get_tmux_target(session, target)

        if text_data:
            try:
                await send_text_to_pane(runtime, tmux_t, text_data)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        if send_enter:
            try:
                await runtime.send_keys(tmux_t, "Enter")
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        app.state.last_ws_input_time = time.time()
        return {"ok": True}

    @app.post("/api/terminal/resize")
    async def terminal_resize(
        cols: int = Query(80),
        rows: int = Query(24),
        _auth=Depends(deps.verify_token),
    ):
        """Resize the PTY."""
        runtime = app.state.runtime
        if not runtime.has_fd:
            return JSONResponse({"error": "No active terminal"}, status_code=400)
        runtime.set_size(cols, rows)
        logger.info(f"Terminal resized to {cols}x{rows} (via SSE POST)")
        return {"ok": True}

    @app.post("/api/terminal/mode")
    async def terminal_mode(
        mode: str = Query("tail"),
        _auth=Depends(deps.verify_token),
    ):
        """Switch client output mode (tail|full)."""
        if mode not in ("tail", "full"):
            return JSONResponse({"error": "Invalid mode, must be 'tail' or 'full'"}, status_code=400)

        client = app.state.active_client
        if client is None:
            return JSONResponse({"error": "No active client"}, status_code=400)

        old_mode = getattr(client, "client_mode", "tail")
        if hasattr(client, "client_mode"):
            client.client_mode = mode

        logger.info(f"[MODE] {old_mode} -> {mode} (via SSE POST)")

        # When switching to full mode, send capture-pane snapshot
        if mode == "full" and client.is_connected:
            runtime = app.state.runtime
            try:
                session = app.state.current_session
                target = app.state.active_target
                snapshot = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["tmux", "capture-pane", "-p", "-e", "-t",
                         get_tmux_target(session, target)],
                        capture_output=True, text=True, timeout=2,
                    ).stdout or ""
                )
                if snapshot:
                    await client.send_text("\x1b[2J\x1b[H" + snapshot)
                    logger.info(f"[MODE] Sent capture-pane snapshot ({len(snapshot)} bytes)")
            except Exception as e:
                logger.warning(f"[MODE] capture-pane catchup failed: {e}")

            if runtime.child_pid:
                try:
                    os.kill(runtime.child_pid, signal.SIGWINCH)
                except ProcessLookupError:
                    pass

        return {"ok": True}

    @app.post("/api/terminal/ping")
    async def terminal_ping(
        _auth=Depends(deps.verify_token),
    ):
        """Synchronous ping — confirms server is alive."""
        return {"ok": True, "ts": time.time()}
