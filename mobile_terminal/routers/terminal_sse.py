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
)

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

        # Close any existing connection (single-client mode, cross-transport)
        if app.state.active_client is not None:
            try:
                await app.state.active_client.close(code=4002)
                logger.info("Closed previous client connection (cross-transport)")
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

        # Clear screen trigger (same as WS handler)
        await sink.send_text("\x1b[2J\x1b[H")

        # -- shared state for background tasks --
        pty_died = False
        connection_closed = False
        recent_buffer = bytearray()
        RECENT_BUFFER_MAX = 64 * 1024
        tail_seq = 0
        TAIL_INTERVAL = 0.2
        last_target_epoch = app.state.target_epoch
        pty_batch = bytearray()
        pty_batch_flush_time = time.time()

        # ---- background tasks (adapted from terminal_io.py) ---------------

        async def read_from_terminal():
            """Read from PTY and forward to SSE sink."""
            nonlocal pty_died, connection_closed, recent_buffer, pty_batch, pty_batch_flush_time
            loop = asyncio.get_event_loop()
            FLUSH_INTERVAL = 0.200
            FLUSH_MAX_BYTES = 2048

            while app.state.active_client is sink and not connection_closed:
                try:
                    data = await loop.run_in_executor(
                        None, lambda: runtime.pty_read(4096)
                    )
                    if not data:
                        # Timeout — no data available, loop again
                        continue

                    app.state.input_queue.update_output_ts()
                    output_buffer.write(data)

                    recent_buffer.extend(data)
                    if len(recent_buffer) > RECENT_BUFFER_MAX:
                        recent_buffer = recent_buffer[-RECENT_BUFFER_MAX:]

                    if sink.client_mode == "full":
                        pty_batch.extend(data)
                        if len(pty_batch) > FLUSH_MAX_BYTES * 4:
                            keep_from = len(pty_batch) - FLUSH_MAX_BYTES
                            while keep_from < len(pty_batch) and (pty_batch[keep_from] & 0xC0) == 0x80:
                                keep_from += 1
                            pty_batch = pty_batch[keep_from:]

                        now = time.time()
                        if (now - pty_batch_flush_time) >= FLUSH_INTERVAL:
                            if app.state.active_client is sink and pty_batch and not connection_closed:
                                cut_pos = find_utf8_boundary(pty_batch, FLUSH_MAX_BYTES)
                                send_data = bytes(pty_batch[:cut_pos])
                                pty_batch = pty_batch[cut_pos:]
                                await sink.send_bytes(send_data)
                                pty_batch_flush_time = now

                except EOFError as e:
                    logger.warning(f"PTY EOF: {e}")
                    pty_died = True
                    break
                except Exception as e:
                    if connection_closed or "close" in str(e).lower():
                        break
                    if app.state.active_client is sink:
                        logger.error(f"Error reading from terminal: {e}")
                    break

            # Flush remaining
            if sink.client_mode == "full" and pty_batch and app.state.active_client is sink and not connection_closed:
                try:
                    await sink.send_bytes(bytes(pty_batch))
                except Exception:
                    pass

        async def server_keepalive():
            """Send SSE keepalive comments every 25s."""
            nonlocal connection_closed
            KEEPALIVE_INTERVAL = 25
            while app.state.active_client is sink and not connection_closed:
                try:
                    await asyncio.sleep(KEEPALIVE_INTERVAL)
                    if app.state.active_client is sink and not connection_closed:
                        await sink._enqueue(": keepalive\n\n")
                except Exception:
                    break

        async def tail_sender():
            """Send periodic tail updates when in tail mode."""
            nonlocal tail_seq, connection_closed, recent_buffer, last_target_epoch
            perm_check_counter = 0
            candidate_check_counter = 0
            while app.state.active_client is sink and not connection_closed:
                try:
                    await asyncio.sleep(TAIL_INTERVAL)

                    # Clear stale output when pane switches
                    current_epoch = app.state.target_epoch
                    if current_epoch != last_target_epoch:
                        recent_buffer = bytearray()
                        last_target_epoch = current_epoch
                        tail_seq = 0

                    if sink.client_mode == "tail" and recent_buffer and not connection_closed:
                        try:
                            text = bytes(recent_buffer[-8192:]).decode("utf-8", errors="replace")
                            plain = strip_ansi(text)
                            lines = plain.split("\n")[-50:]
                            lines = [l for l in lines if "How is Claude doing this session" not in l]
                            tail_text = "\n".join(lines)
                            tail_seq += 1
                            await sink.send_json({
                                "type": "tail",
                                "text": tail_text,
                                "seq": tail_seq,
                            })
                        except Exception as e:
                            if "close" in str(e).lower():
                                connection_closed = True
                                break
                            logger.debug(f"Tail extraction error: {e}")

                    perm_check_counter += 1
                    if perm_check_counter >= 5 and not connection_closed:
                        perm_check_counter = 0
                        try:
                            detector = app.state.permission_detector
                            session = app.state.current_session
                            target = app.state.active_target
                            if session and detector.log_file:
                                tmux_t = get_tmux_target(session, target)
                                perm = await asyncio.get_event_loop().run_in_executor(
                                    None, detector.check_sync, session, target, tmux_t
                                )
                                if perm:
                                    from mobile_terminal.permission_policy import normalize_request
                                    policy = app.state.permission_policy
                                    req = normalize_request(perm, deps.get_current_repo_path())
                                    decision = policy.evaluate(req)
                                    policy.audit(req, decision)

                                    if decision.action == "allow":
                                        await runtime.send_keys(tmux_t, "y", literal=True)
                                        await runtime.send_keys(tmux_t, "Enter")
                                        await deps.send_typed(sink, "permission_auto",
                                            {"decision": "allow", "tool": req.tool,
                                             "target": req.target, "reason": decision.reason},
                                            level="info")
                                    elif decision.action == "deny":
                                        await runtime.send_keys(tmux_t, "n", literal=True)
                                        await runtime.send_keys(tmux_t, "Enter")
                                        await deps.send_typed(sink, "permission_auto",
                                            {"decision": "deny", "tool": req.tool,
                                             "target": req.target, "reason": decision.reason},
                                            level="warning")
                                    else:
                                        perm["repo"] = str(deps.get_current_repo_path() or "")
                                        perm["risk"] = req.risk
                                        await deps.send_typed(sink, "permission_request", perm, level="urgent")
                        except Exception as e:
                            logger.debug(f"Permission check error: {e}")

                    # Check for backlog candidates every ~2s (10 ticks at 200ms)
                    candidate_check_counter += 1
                    if candidate_check_counter >= 10 and not connection_closed:
                        candidate_check_counter = 0
                        try:
                            cdet = app.state.candidate_detector
                            if cdet.log_file:
                                session = app.state.current_session or ""
                                pane_id = app.state.active_target or ""
                                raw = await asyncio.get_event_loop().run_in_executor(
                                    None, cdet.check_sync, session, pane_id
                                )
                                if raw:
                                    from uuid import uuid4
                                    from dataclasses import asdict
                                    from mobile_terminal.models import BacklogCandidate
                                    project = str(deps.get_current_repo_path() or "")
                                    cstore = app.state.candidate_store
                                    for c in raw:
                                        candidate = BacklogCandidate(
                                            id=str(uuid4()), summary=c["summary"],
                                            prompt=c["prompt"], source_tool=c["source_tool"],
                                            detected_at=time.time(), session=session,
                                            pane_id=pane_id, content_hash=c["hash"],
                                        )
                                        added = cstore.add(project, candidate)
                                        if added:
                                            await deps.send_typed(
                                                sink, "backlog_candidate",
                                                {"action": "new", "candidate": asdict(added)},
                                                level="info",
                                            )
                        except Exception as e:
                            logger.debug(f"Candidate check error: {e}")
                except Exception:
                    break

        async def desktop_activity_monitor():
            """Detect desktop keyboard activity in tmux session (1.5s polling)."""
            last_hash = 0
            desktop_active = False
            desktop_since = 0
            while app.state.active_client is sink and not connection_closed:
                await asyncio.sleep(1.5)
                try:
                    session = app.state.current_session
                    target = app.state.active_target
                    if not session:
                        continue
                    tmux_target = get_tmux_target(session, target)

                    def _capture_desktop():
                        r = subprocess.run(
                            ["tmux", "capture-pane", "-t", tmux_target, "-p", "-S", "-5"],
                            capture_output=True, text=True, timeout=1,
                        )
                        return r.stdout

                    stdout = await asyncio.get_event_loop().run_in_executor(
                        None, _capture_desktop
                    )
                    current_hash = hash(stdout)
                    if current_hash != last_hash:
                        last_hash = current_hash
                        time_since_ws = time.time() - app.state.last_ws_input_time
                        if time_since_ws > 1.5 and not desktop_active:
                            desktop_active = True
                            desktop_since = time.time()
                            await deps.send_typed(sink, "device_state",
                                                  {"desktop_active": True}, level="info")
                        elif time_since_ws <= 1.5 and desktop_active:
                            desktop_active = False
                            await deps.send_typed(sink, "device_state",
                                                  {"desktop_active": False}, level="info")
                    if desktop_active and (time.time() - desktop_since) > 10:
                        desktop_active = False
                        await deps.send_typed(sink, "device_state",
                                              {"desktop_active": False}, level="info")
                except Exception:
                    pass

        # ---- SSE generator ---------------------------------------------------

        async def event_generator():
            nonlocal connection_closed

            # Spawn background tasks
            read_task = asyncio.create_task(read_from_terminal())
            app.state.read_task = read_task
            keepalive_task = asyncio.create_task(server_keepalive())
            tail_task = asyncio.create_task(tail_sender())
            desktop_task = asyncio.create_task(desktop_activity_monitor())

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
                connection_closed = True
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
        target = app.state.active_target
        tmux_t = get_tmux_target(session, target)

        if text_data:
            try:
                await runtime.send_keys(tmux_t, text_data, literal=True)
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
