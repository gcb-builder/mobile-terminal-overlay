"""Routes for terminal I/O (send, sendkey, capture, snapshot, WebSocket)."""
import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import (
    strip_ansi, find_utf8_boundary,
    run_subprocess, get_tmux_target,
    get_cached_capture, set_cached_capture,
    set_terminal_size,
)

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register terminal I/O routes and WebSocket handler."""

    @app.get("/api/terminal/capture")
    async def capture_terminal(
        request: Request,
        _auth=Depends(deps.verify_token),
        lines: int = Query(50),
        session: Optional[str] = Query(None),
        pane: int = Query(0),
    ):
        """
        Capture recent terminal output from tmux pane.

        Uses tmux capture-pane to get scrollback buffer.
        Returns last N lines of terminal content.
        Also returns pane_title which Claude Code uses to signal its state.

        Params:
            session: tmux session name (defaults to current_session)
            pane: pane index within session (default 0)
            lines: number of lines to capture (default 50)

        Includes 300ms cache to prevent DoS from rapid polling.
        """
        # Log client ID for debugging duplicate requests
        client_id = request.headers.get('X-Client-ID', 'unknown')[:8]
        logger.debug(f"[{client_id}] GET /api/terminal/capture lines={lines}")


        # Use provided session or fall back to current
        target_session = session or app.state.current_session
        if not target_session:
            return {"content": "", "error": "No session"}

        # Use active_target if no explicit pane provided, otherwise use params
        if app.state.active_target and pane == 0 and session is None:
            # Use active target - convert "window:pane" to "session:window.pane"
            target = get_tmux_target(target_session, app.state.active_target)
            pane_id = app.state.active_target
        else:
            # Use explicit params
            pane_id = str(pane)
            target = f"{target_session}:{0}.{pane}"

        # Check cache first
        cached = get_cached_capture(target_session, pane_id, lines)
        if cached:
            return cached

        try:
            # Capture last N lines from tmux pane
            result = await run_subprocess(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Get pane title - Claude Code sets this to indicate state
            # e.g., "✳ Signal Detection Pending" when waiting for input
            pane_title = ""
            try:
                title_result = await run_subprocess(
                    ["tmux", "display-message", "-p", "-t", target, "#{pane_title}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if title_result.returncode == 0:
                    pane_title = title_result.stdout.strip()
            except Exception:
                pass

            if result.returncode == 0:
                response = {
                    "content": result.stdout,
                    "lines": lines,
                    "pane_title": pane_title,
                    "session": target_session,
                    "pane": pane,
                }
                set_cached_capture(target_session, pane_id, lines, response)
                return response
            else:
                # Target missing or invalid
                if "can't find" in result.stderr.lower() or "no such" in result.stderr.lower():
                    return JSONResponse(
                        {"error": f"Target not found: {target}", "session": target_session, "pane": pane},
                        status_code=409,
                    )
                return {"content": "", "error": result.stderr, "pane_title": pane_title}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Capture timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Failed to capture terminal: {e}")
            return {"content": "", "error": str(e)}

    @app.get("/api/terminal/snapshot")
    async def terminal_snapshot(
        _auth=Depends(deps.verify_token),
        target: Optional[str] = Query(None),
    ):
        """
        Get terminal snapshot for resync after queue overflow.

        Returns tmux capture-pane with ANSI escape sequences (-e) for
        accurate screen reproduction. Limited to 80 lines max.
        Used by client when terminal render queue overflows.
        """

        session_name = app.state.current_session
        if not session_name:
            return {"content": "", "error": "No session"}

        # Use provided target or active target
        if target:
            tmux_target = get_tmux_target(session_name, target)
        elif app.state.active_target:
            tmux_target = get_tmux_target(session_name, app.state.active_target)
        else:
            tmux_target = session_name

        try:
            # Capture with ANSI escape sequences for accurate screen state
            # Limit to 80 lines to keep payload reasonable
            result = await run_subprocess(
                ["tmux", "capture-pane", "-p", "-e", "-S", "-80", "-t", tmux_target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                content = result.stdout or ""
                # If still too large, reduce further
                if len(content) > 50000:  # 50KB max
                    result = await run_subprocess(
                        ["tmux", "capture-pane", "-p", "-e", "-S", "-40", "-t", tmux_target],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    content = result.stdout or "" if result.returncode == 0 else ""
                logger.info(f"Terminal snapshot: {len(content)} chars")
                return {"content": content, "target": tmux_target}
            else:
                logger.warning(f"Snapshot failed: {result.stderr}")
                return {"content": "", "error": result.stderr}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Snapshot timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Snapshot error: {e}")
            return {"content": "", "error": str(e)}

    @app.post("/api/send")
    async def send_line(
        text: str = Query(...),
        session: str = Query(...),
        msg_id: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """
        Send a line of text to the terminal with Enter.
        Uses the InputQueue for serialized, atomic writes with ACKs.
        """

        # Validate session matches current
        if session != app.state.current_session:
            return JSONResponse({
                "error": "Session mismatch",
                "expected": app.state.current_session,
                "got": session
            }, status_code=400)

        if app.state.master_fd is None:
            return JSONResponse({"error": "No active terminal"}, status_code=400)

        # Atomic write: text + carriage return
        data = (text + "\r").encode("utf-8")

        # Queue the send (will wait for quiet period and send ACK)
        success = await app.state.input_queue.send(
            msg_id,
            data,
            app.state.master_fd,
            app.state.active_websocket
        )

        if success:
            return {"status": "ok", "id": msg_id}
        else:
            return JSONResponse({"error": "Send timeout", "id": msg_id}, status_code=504)

    @app.post("/api/sendkey")
    async def send_key(
        key: str = Query(...),
        session: str = Query(...),
        msg_id: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """
        Send a control key using tmux send-keys.
        Supports: C-c, C-d, C-z, C-l, Tab, Escape, Enter, Up, Down, Left, Right, etc.
        """

        # Validate session matches current
        if session != app.state.current_session:
            return JSONResponse({
                "error": "Session mismatch",
                "expected": app.state.current_session,
                "got": session
            }, status_code=400)

        # Map common key names to tmux send-keys format
        key_map = {
            "ctrl-c": "C-c",
            "ctrl-d": "C-d",
            "ctrl-z": "C-z",
            "ctrl-l": "C-l",
            "ctrl-a": "C-a",
            "ctrl-e": "C-e",
            "ctrl-w": "C-w",
            "ctrl-u": "C-u",
            "ctrl-k": "C-k",
            "ctrl-r": "C-r",
            "ctrl-o": "C-o",
            "ctrl-b": "C-b",
            "tab": "Tab",
            "escape": "Escape",
            "esc": "Escape",
            "enter": "Enter",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "pageup": "PageUp",
            "pagedown": "PageDown",
            "home": "Home",
            "end": "End",
        }

        tmux_key = key_map.get(key.lower(), key)
        # Use active target pane if set, otherwise default to 0.0
        target = get_tmux_target(session, app.state.active_target)

        try:
            result = await run_subprocess(
                ["tmux", "send-keys", "-t", target, tmux_key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return JSONResponse({
                    "error": f"tmux send-keys failed: {result.stderr}",
                    "id": msg_id
                }, status_code=500)

            # Send ACK via WebSocket if connected
            if app.state.active_websocket:
                try:
                    await app.state.active_websocket.send_json({
                        "type": "ack",
                        "id": msg_id
                    })
                except Exception:
                    pass

            return {"status": "ok", "id": msg_id, "key": tmux_key}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "tmux command timeout", "id": msg_id}, status_code=504)
        except Exception as e:
            return JSONResponse({"error": str(e), "id": msg_id}, status_code=500)

    @app.websocket("/ws/terminal")
    async def terminal_websocket(websocket: WebSocket, _auth=Depends(deps.verify_token)):
        """WebSocket endpoint for terminal I/O."""
        _ws_start = time.time()
        logger.info(f"[TIMING] WebSocket /ws/terminal START")

        # Use lock to prevent concurrent connection setup
        async with app.state.ws_connect_lock:
            # Rate limit connections - minimum 500ms between accepts
            now = time.time()
            elapsed = now - app.state.last_ws_connect
            if elapsed < 0.5:
                logger.info(f"Rate limiting WebSocket connection ({elapsed:.2f}s since last)")
                await websocket.close(code=4004)  # 4004 = rate limited
                return
            app.state.last_ws_connect = now

            await websocket.accept()
            logger.info("WebSocket connection accepted")

            # Close any existing connection (single client mode)
            if app.state.active_websocket is not None:
                try:
                    await app.state.active_websocket.close(code=4002)
                    logger.info("Closed previous WebSocket connection")
                except Exception:
                    pass
            if app.state.read_task is not None:
                app.state.read_task.cancel()
                app.state.read_task = None

            app.state.active_websocket = websocket
        logger.info(f"[TIMING] WebSocket lock+accept took {time.time()-_ws_start:.3f}s")

        # Spawn tmux if not already running
        _spawn_start = time.time()
        if app.state.master_fd is None:
            try:
                session_name = app.state.current_session
                master_fd, child_pid = app.state.spawn_tmux(session_name)
                app.state.master_fd = master_fd
                app.state.child_pid = child_pid
                logger.info(f"Spawned tmux session: {session_name}")
            except Exception as e:
                logger.error(f"Failed to spawn tmux: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})
                await websocket.close()
                return
        logger.info(f"[TIMING] spawn_tmux took {time.time()-_spawn_start:.3f}s")
        master_fd = app.state.master_fd
        output_buffer = app.state.output_buffer

        # Send hello handshake FIRST - client expects this within 2s
        # Must be sent before capture-pane which can be slow
        _hello_start = time.time()
        try:
            hello_msg = {
                "type": "hello",
                "session": app.state.current_session,
                "pid": app.state.child_pid,
                "started_at": int(time.time()),
            }
            await websocket.send_json(hello_msg)
            logger.info(f"Sent hello handshake: {hello_msg}")
        except Exception as e:
            logger.error(f"Failed to send hello: {e}")
            await websocket.close(code=4500)
            return
        logger.info(f"[TIMING] hello handshake took {time.time()-_hello_start:.3f}s")

        # Don't send capture-pane history on initial connect
        # Default mode is "tail" which uses lightweight JSON updates
        # History will be sent as catchup when client switches to "full" mode
        # Just send clear screen to trigger client overlay hide
        await websocket.send_text("\x1b[2J\x1b[H")
        logger.info(f"[TIMING] WebSocket setup TOTAL took {time.time()-_ws_start:.3f}s")

        # Track PTY death for proper close code
        pty_died = False
        # Track connection closed to prevent send-after-close errors
        connection_closed = False

        # Client output mode: "tail" (default) or "full"
        # In tail mode: don't forward raw PTY bytes, send periodic tail snapshots
        # In full mode: forward raw PTY bytes for full terminal rendering
        client_mode = "tail"
        # Ring buffer for recent output (for tail extraction and mode switch catchup)
        recent_buffer = bytearray()
        RECENT_BUFFER_MAX = 64 * 1024  # 64KB of recent output
        # Tail state
        tail_seq = 0
        TAIL_INTERVAL = 0.2  # Send tail updates every 200ms
        # Shared PTY output batch (cleared on mode switch to prevent stale data)
        pty_batch = bytearray()
        pty_batch_flush_time = time.time()

        # Create tasks for bidirectional I/O
        async def read_from_terminal():
            """Read from terminal and send to WebSocket with batching.

            CRITICAL: PTY is ALWAYS drained regardless of client_mode.
            - In 'full' mode: forward raw bytes to WebSocket (coalesced)
            - In 'tail' mode: skip WebSocket send (tail_sender handles updates)

            Coalescing: accumulate PTY bytes and flush every 25ms or 16KB
            to reduce WS message frequency and client pressure.
            """
            nonlocal pty_died, connection_closed, recent_buffer, pty_batch, pty_batch_flush_time
            loop = asyncio.get_event_loop()
            # Coalescing parameters - balance latency vs throughput
            # Aggressive rate limiting for mobile debugging
            FLUSH_INTERVAL = 0.200  # 200ms = 5 FPS max
            FLUSH_MAX_BYTES = 2048   # 2KB max per message (10KB/s total)

            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    # Non-blocking read with select-like behavior
                    # ALWAYS read - never pause PTY drain
                    data = await loop.run_in_executor(
                        None, lambda: os.read(master_fd, 4096)
                    )
                    if not data:
                        # PTY returned EOF - terminal died
                        logger.warning("PTY returned EOF - terminal process died")
                        pty_died = True
                        break

                    # Update input queue timestamp (for quiet-wait logic)
                    app.state.input_queue.update_output_ts()

                    # Store in ring buffer for future reconnects
                    output_buffer.write(data)

                    # Store in recent buffer for tail extraction and mode switch catchup
                    recent_buffer.extend(data)
                    if len(recent_buffer) > RECENT_BUFFER_MAX:
                        # Trim to last RECENT_BUFFER_MAX bytes
                        recent_buffer = recent_buffer[-RECENT_BUFFER_MAX:]

                    # Only forward to WebSocket in 'full' mode
                    if client_mode == "full":
                        # Add to batch (cap size to prevent memory issues)
                        pty_batch.extend(data)
                        if len(pty_batch) > FLUSH_MAX_BYTES * 4:
                            # Drop old data if accumulating too fast
                            # Keep from a UTF-8 safe boundary
                            keep_from = len(pty_batch) - FLUSH_MAX_BYTES
                            # Find start of a valid UTF-8 character
                            while keep_from < len(pty_batch) and (pty_batch[keep_from] & 0xC0) == 0x80:
                                keep_from += 1
                            pty_batch = pty_batch[keep_from:]

                        # ONLY flush on time interval (enforces rate limit for mobile)
                        # This prevents flooding the client even if PTY is very active
                        now = time.time()
                        if (now - pty_batch_flush_time) >= FLUSH_INTERVAL:
                            if app.state.active_websocket == websocket and pty_batch and not connection_closed:
                                # Send at most FLUSH_MAX_BYTES per interval
                                # Use UTF-8 safe boundary to avoid splitting multi-byte chars
                                cut_pos = find_utf8_boundary(pty_batch, FLUSH_MAX_BYTES)
                                send_data = bytes(pty_batch[:cut_pos])
                                pty_batch = pty_batch[cut_pos:]
                                await websocket.send_bytes(send_data)
                                pty_batch_flush_time = now

                except Exception as e:
                    # Ignore send-after-close errors (expected during disconnect)
                    if connection_closed or "after sending" in str(e) or "websocket.close" in str(e):
                        break
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error reading from terminal: {e}")
                    break

            # Flush remaining data (only in full mode)
            if client_mode == "full" and pty_batch and app.state.active_websocket == websocket and not connection_closed:
                try:
                    await websocket.send_bytes(bytes(pty_batch))
                except Exception:
                    pass

        async def write_to_terminal():
            """Read from WebSocket and write to terminal."""
            nonlocal client_mode, pty_batch, pty_batch_flush_time
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    message = await websocket.receive()

                    # Check message type safely
                    if not isinstance(message, dict):
                        continue

                    msg_type = message.get("type", "")
                    if msg_type == "websocket.disconnect":
                        break

                    if "bytes" in message:
                        os.write(master_fd, message["bytes"])
                        app.state.last_ws_input_time = time.time()
                    elif "text" in message:
                        text = message["text"]
                        logger.info(f"Received text message: {text[:100]}")
                        # Handle JSON messages (resize, input)
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                msg_type = data.get("type")
                                if msg_type == "resize":
                                    cols = data.get("cols", 80)
                                    rows = data.get("rows", 24)
                                    logger.info(f"Resize request: {cols}x{rows}, fd={master_fd}, pid={app.state.child_pid}")
                                    set_terminal_size(
                                        master_fd,
                                        cols,
                                        rows,
                                        app.state.child_pid,
                                    )
                                    logger.info(f"Terminal resized to {cols}x{rows}")
                                elif msg_type == "input":
                                    input_data = data.get("data")
                                    if input_data:
                                        os.write(master_fd, input_data.encode())
                                        app.state.last_ws_input_time = time.time()
                                elif msg_type == "ping":
                                    # Respond to heartbeat ping with pong
                                    if not connection_closed:
                                        await websocket.send_json({"type": "pong"})
                                elif msg_type == "pong":
                                    # Client responding to server_ping - connection is alive
                                    pass  # No action needed, connection confirmed alive
                                elif msg_type == "text":
                                    # Atomic text send via tmux send-keys (not PTY write)
                                    # This avoids interleaving with PTY output stream
                                    text_data = data.get("text", "")
                                    send_enter = data.get("enter", False)
                                    loop = asyncio.get_event_loop()
                                    session = app.state.current_session
                                    target = app.state.active_target
                                    tmux_t = get_tmux_target(session, target)
                                    if text_data:
                                        try:
                                            await loop.run_in_executor(
                                                None,
                                                lambda: subprocess.run(
                                                    ["tmux", "send-keys", "-t", tmux_t, "-l", text_data],
                                                    timeout=3, check=True,
                                                ),
                                            )
                                        except Exception as e:
                                            logger.warning(f"tmux send-keys failed: {e}")
                                    if send_enter:
                                        try:
                                            await loop.run_in_executor(
                                                None,
                                                lambda: subprocess.run(
                                                    ["tmux", "send-keys", "-t", tmux_t, "Enter"],
                                                    timeout=3, check=True,
                                                ),
                                            )
                                        except Exception as e:
                                            logger.warning(f"tmux send-keys Enter failed: {e}")
                                    app.state.last_ws_input_time = time.time()
                                elif msg_type == "set_mode":
                                    # Client requests output mode change
                                    new_mode = data.get("mode", "tail")
                                    if new_mode in ("tail", "full"):
                                        old_mode = client_mode
                                        client_mode = new_mode
                                        logger.info(f"[MODE] {old_mode} -> {client_mode}")
                                        # When switching to full mode:
                                        # Send capture-pane snapshot as immediate catchup,
                                        # then SIGWINCH for live forwarding.
                                        # The snapshot fixes the race where resize SIGWINCH
                                        # fires while still in tail mode (data lost).
                                        if new_mode == "full" and not connection_closed:
                                            pty_batch.clear()
                                            pty_batch_flush_time = 0
                                            # Send capture-pane snapshot so client has
                                            # current screen content immediately
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
                                                if snapshot and not connection_closed:
                                                    # Clear screen + send snapshot for clean render
                                                    await websocket.send_text("\x1b[2J\x1b[H" + snapshot)
                                                    logger.info(f"[MODE] Sent capture-pane snapshot ({len(snapshot)} bytes)")
                                            except Exception as e:
                                                logger.warning(f"[MODE] capture-pane catchup failed: {e}")
                                            # Also send SIGWINCH so live PTY forwarding
                                            # picks up from the correct state
                                            if app.state.child_pid:
                                                try:
                                                    os.kill(app.state.child_pid, signal.SIGWINCH)
                                                except ProcessLookupError:
                                                    pass
                                            logger.info("[MODE] Full mode activated with snapshot catchup")
                                elif msg_type == "term_subscribe":
                                    # Legacy: treat as set_mode full
                                    client_mode = "full"
                                    logger.info("Client subscribed to terminal view (mode=full)")
                                    if not connection_closed:
                                        pty_batch.clear()
                                        pty_batch_flush_time = time.time()
                                elif msg_type == "term_unsubscribe":
                                    # Legacy: treat as set_mode tail
                                    client_mode = "tail"
                                    logger.info("Client unsubscribed from terminal view (mode=tail)")
                            else:
                                # JSON but not dict, treat as plain text
                                os.write(master_fd, text.encode())
                        except (json.JSONDecodeError, TypeError, KeyError):
                            # Plain text input
                            os.write(master_fd, text.encode())

                except WebSocketDisconnect:
                    break
                except (OSError, IOError) as e:
                    # Terminal write errors are fatal (terminal closed, etc.)
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error writing to terminal: {e}")
                    break
                except Exception as e:
                    # Log but continue on other errors (malformed messages, etc.)
                    if app.state.active_websocket == websocket:
                        logger.warning(f"Ignoring malformed message: {e}")
                    continue

        async def server_keepalive():
            """Send periodic pings from server to keep connection alive."""
            SERVER_PING_INTERVAL = 20  # Send ping every 20s from server
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    await asyncio.sleep(SERVER_PING_INTERVAL)
                    if app.state.active_websocket == websocket and not connection_closed:
                        # Send server-initiated ping (client will respond with pong)
                        await websocket.send_json({"type": "server_ping"})
                except Exception:
                    break

        async def tail_sender():
            """Send periodic tail updates when in tail mode.

            Extracts last ~50 lines from recent_buffer, strips ANSI,
            and sends as JSON for lightweight Log view rendering.
            Also checks for pending permission requests (v2 messages).
            """
            nonlocal tail_seq, connection_closed
            perm_check_counter = 0
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    await asyncio.sleep(TAIL_INTERVAL)
                    if client_mode == "tail" and recent_buffer and not connection_closed:
                        # Extract last portion and decode
                        try:
                            text = bytes(recent_buffer[-8192:]).decode('utf-8', errors='replace')
                            # Strip ANSI and get last 50 lines
                            plain = strip_ansi(text)
                            lines = plain.split('\n')[-50:]
                            tail_text = '\n'.join(lines)
                            tail_seq += 1
                            await websocket.send_json({
                                "type": "tail",
                                "text": tail_text,
                                "seq": tail_seq
                            })
                        except Exception as e:
                            if "close" in str(e).lower() or "after sending" in str(e):
                                connection_closed = True
                                break
                            logger.debug(f"Tail extraction error: {e}")

                    # Check for permission requests every ~1s (5 ticks at 200ms)
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
                                    await deps.send_typed(websocket, "permission_request", perm, level="urgent")
                        except Exception as e:
                            logger.debug(f"Permission check error: {e}")
                except Exception:
                    break

        async def desktop_activity_monitor():
            """Detect desktop keyboard activity in tmux session (1.5s polling)."""
            last_hash = 0
            desktop_active = False
            desktop_since = 0
            while app.state.active_websocket == websocket and not connection_closed:
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
                            capture_output=True, text=True, timeout=1
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
                            await deps.send_typed(websocket, "device_state",
                                             {"desktop_active": True}, level="info")
                        elif time_since_ws <= 1.5 and desktop_active:
                            desktop_active = False
                            await deps.send_typed(websocket, "device_state",
                                             {"desktop_active": False}, level="info")
                    if desktop_active and (time.time() - desktop_since) > 10:
                        desktop_active = False
                        await deps.send_typed(websocket, "device_state",
                                         {"desktop_active": False}, level="info")
                except Exception:
                    pass

        # Run all tasks concurrently
        read_task = asyncio.create_task(read_from_terminal())
        app.state.read_task = read_task
        write_task = asyncio.create_task(write_to_terminal())
        keepalive_task = asyncio.create_task(server_keepalive())
        tail_task = asyncio.create_task(tail_sender())
        desktop_task = asyncio.create_task(desktop_activity_monitor())

        try:
            await asyncio.gather(read_task, write_task, keepalive_task, tail_task, desktop_task)
        except asyncio.CancelledError:
            # Normal termination when connection is replaced or closed
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            # Signal tasks to stop sending before canceling
            connection_closed = True
            read_task.cancel()
            write_task.cancel()
            keepalive_task.cancel()
            tail_task.cancel()
            desktop_task.cancel()
            if app.state.active_websocket == websocket:
                app.state.active_websocket = None
                app.state.read_task = None

            # Close with appropriate code
            if pty_died:
                logger.warning("Closing WebSocket with code 4500 (PTY died)")
                try:
                    await websocket.close(code=4500, reason="PTY died")
                except Exception:
                    pass
                # Clear PTY state so next connection recreates it
                app.state.master_fd = None
                app.state.child_pid = None

            logger.info("WebSocket connection closed")
