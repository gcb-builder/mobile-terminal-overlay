"""Shared per-connection terminal-session runners.

Both the WebSocket handler (``routers/terminal_io.py``) and the SSE handler
(``routers/terminal_sse.py``) attach a connected client to one tmux session
and run the same set of background tasks: drain the PTY, emit periodic tail
snapshots, run permission/candidate detection, and watch for desktop-side
typing activity. Before this module those tasks were implemented twice
(~220 lines of duplicate code) and behavior drift between the two copies
caused real bugs (e.g. when one transport got a fix and the other didn't).

This module owns the **transport-agnostic** runners only. Each transport
keeps the parts that are genuinely transport-shaped:

  - WebSocket: ``server_keepalive`` (bidirectional ping/pong with ghost
    detection) and ``write_to_terminal`` (in-band JSON message dispatch).
  - SSE: ``server_keepalive`` (unidirectional ``: keepalive`` comments)
    and the POST endpoints that serve as the input channel.

Those two are intentionally NOT extracted — sharing them would force a
worse abstraction than letting them live where they belong.

A ``TerminalSessionState`` dataclass holds the per-connection mutable
state the runners need to read/write. It is **scoped to one attached
session** — do not stuff app-global concerns into it.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from mobile_terminal.helpers import (
    find_utf8_boundary,
    get_tmux_target,
    strip_ansi,
)
from mobile_terminal.pane_buffer import get_or_create_pane_buffer
from mobile_terminal.transport import ClientSink

logger = logging.getLogger(__name__)


# Tunables — match the historical inline values that the WS/SSE handlers
# used. Kept here so both transports get changes in lock-step.
RECENT_BUFFER_MAX = 64 * 1024  # 64 KB ring of recent PTY output
TAIL_INTERVAL = 0.2            # tail/perm/candidate poll cadence (seconds)
PTY_FLUSH_INTERVAL = 0.200     # min seconds between binary flushes
PTY_FLUSH_MAX_BYTES = 2048     # max bytes per binary flush
DESKTOP_POLL_INTERVAL = 1.5    # capture-pane cadence for desktop-typing detection
DESKTOP_IDLE_TIMEOUT = 10.0    # auto-clear desktop_active after this many seconds


@dataclass
class TerminalSessionState:
    """Per-connection mutable state passed to the shared runners.

    Lifetime: one attached client (one WS connection or one SSE stream).
    Owned and constructed by the transport handler; flags here drive loop
    termination so the handler can signal "we're done" without cancelling
    the tasks (cleaner shutdown, especially around ``finally:`` flush).
    """

    sink: ClientSink                            # WebSocketSink or SSESink
    runtime: Any                                # TmuxRuntime (Any to avoid import cycle)
    output_buffer: Any                          # OutputBuffer (Any: same)
    recent_buffer: bytearray = field(default_factory=bytearray)
    pty_batch: bytearray = field(default_factory=bytearray)
    pty_batch_flush_time: float = field(default_factory=time.time)
    tail_seq: int = 0
    last_target_epoch: int = 0
    pty_died: bool = False
    connection_closed: bool = False


# ── Shared runners ─────────────────────────────────────────────────────


async def read_from_terminal(state: TerminalSessionState, app) -> None:
    """Drain the PTY and forward bytes to the sink (full mode only).

    PTY is ALWAYS drained regardless of mode — in 'tail' mode we just
    don't forward to the client (tail_sender pushes compact JSON
    snapshots instead). The recent ring buffer is fed in both modes so
    tail_sender can extract the last ~50 lines.

    Coalesces output into <= ``PTY_FLUSH_MAX_BYTES`` chunks per
    ``PTY_FLUSH_INTERVAL`` to keep mobile clients from drowning. UTF-8
    boundary respected on cuts so multi-byte chars never split.
    """
    sink = state.sink
    runtime = state.runtime
    loop = asyncio.get_event_loop()

    while app.state.active_client is sink and not state.connection_closed:
        try:
            data = await loop.run_in_executor(
                None, lambda: runtime.pty_read(4096)
            )
            if not data:
                continue  # 1s select timeout, no data — loop

            # Output side bookkeeping (used by quiet-wait / queue drain).
            app.state.input_queue.update_output_ts()

            # Persistent ring buffer for full-mode reconnect catchup.
            state.output_buffer.write(data)

            # Per-pane delta buffer (step 2: silently maintained, used by
            # later steps to serve since=N reconnects without re-shipping
            # the full snapshot). Keyed by current (session, target) at
            # append time — a brief mis-attribution during a pane switch
            # is tolerated since the snapshot fallback covers it.
            try:
                pbuf = get_or_create_pane_buffer(
                    app.state.pane_buffers,
                    app.state.current_session,
                    app.state.active_target,
                )
                pbuf.append(data)
            except Exception as e:
                logger.debug(f"pane buffer append failed: {e}")

            # Recent buffer for tail extraction + mode-switch snapshot.
            state.recent_buffer.extend(data)
            if len(state.recent_buffer) > RECENT_BUFFER_MAX:
                state.recent_buffer = state.recent_buffer[-RECENT_BUFFER_MAX:]

            # In tail mode we're done — tail_sender handles the client.
            if sink.client_mode != "full":
                continue

            # Full mode: stage into pty_batch, flush on interval.
            state.pty_batch.extend(data)
            if len(state.pty_batch) > PTY_FLUSH_MAX_BYTES * 4:
                # Drop oldest if we're falling behind. Walk to next
                # UTF-8 lead byte so we never split a code point.
                keep_from = len(state.pty_batch) - PTY_FLUSH_MAX_BYTES
                while keep_from < len(state.pty_batch) and (state.pty_batch[keep_from] & 0xC0) == 0x80:
                    keep_from += 1
                state.pty_batch = state.pty_batch[keep_from:]

            now = time.time()
            if (now - state.pty_batch_flush_time) >= PTY_FLUSH_INTERVAL:
                if app.state.active_client is sink and state.pty_batch and not state.connection_closed:
                    cut_pos = find_utf8_boundary(state.pty_batch, PTY_FLUSH_MAX_BYTES)
                    send_data = bytes(state.pty_batch[:cut_pos])
                    state.pty_batch = state.pty_batch[cut_pos:]
                    await sink.send_bytes(send_data)
                    state.pty_batch_flush_time = now

        except EOFError as e:
            logger.warning(f"PTY EOF: {e}")
            state.pty_died = True
            break
        except Exception as e:
            # Send-after-close is expected during disconnect — swallow it.
            err = str(e)
            if state.connection_closed or "after sending" in err or "websocket.close" in err:
                break
            if app.state.active_client is sink:
                logger.error(f"Error reading from terminal ({sink.transport_type}): {e}")
            break

    # Final flush (full mode) so the last bytes don't get stranded.
    if (
        sink.client_mode == "full"
        and state.pty_batch
        and app.state.active_client is sink
        and not state.connection_closed
    ):
        try:
            await sink.send_bytes(bytes(state.pty_batch))
        except Exception:
            pass


async def tail_sender(state: TerminalSessionState, app, deps) -> None:
    """Send compact 'tail' snapshots + run permission and candidate detection.

    Loops every ``TAIL_INTERVAL`` seconds. Three things happen per tick:

    1. **Tail snapshot** (only when sink is in 'tail' mode and we have
       buffered output): take the last 8KB of the recent ring, strip
       ANSI, keep the last 50 lines, ship as ``{type: 'tail', text, seq}``.

    2. **Permission detection** (every 5 ticks ≈ 1s): poll the JSONL
       permission detector. If a prompt was emitted, evaluate it against
       the policy — auto-allow / auto-deny / forward to client for manual
       handling.

    3. **Backlog candidate detection** (every 10 ticks ≈ 2s): poll the
       candidate detector. As of PR3b this is a no-op skeleton kept for
       future re-enablement; the call still runs so re-enabling needs no
       wiring change.

    Resets tail state when ``app.state.target_epoch`` changes (pane switch)
    so the new pane doesn't see the old pane's buffer.
    """
    sink = state.sink
    runtime = state.runtime
    perm_check_counter = 0
    candidate_check_counter = 0

    while app.state.active_client is sink and not state.connection_closed:
        try:
            await asyncio.sleep(TAIL_INTERVAL)

            # Pane switched? Drop stale buffer, reset seq.
            current_epoch = app.state.target_epoch
            if current_epoch != state.last_target_epoch:
                state.recent_buffer = bytearray()
                state.last_target_epoch = current_epoch
                state.tail_seq = 0

            # 1) Tail snapshot
            if sink.client_mode == "tail" and state.recent_buffer and not state.connection_closed:
                try:
                    text = bytes(state.recent_buffer[-8192:]).decode("utf-8", errors="replace")
                    plain = strip_ansi(text)
                    lines = plain.split("\n")[-50:]
                    # Filter Claude CLI nag prompt that scrolls in periodically
                    lines = [l for l in lines if "How is Claude doing this session" not in l]
                    tail_text = "\n".join(lines)
                    state.tail_seq += 1
                    await sink.send_json({
                        "type": "tail",
                        "text": tail_text,
                        "seq": state.tail_seq,
                    })
                except Exception as e:
                    err = str(e).lower()
                    if "close" in err or "after sending" in err:
                        state.connection_closed = True
                        break
                    logger.debug(f"Tail extraction error: {e}")

            # 2) Permission detection
            perm_check_counter += 1
            if perm_check_counter >= 5 and not state.connection_closed:
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
                                # Stamp the prompting pane so the client can
                                # send Allow/Deny back to THAT pane, not to
                                # whatever app.state.active_target happens to
                                # be when another client switched panes.
                                perm["source_pane"] = target
                                await deps.send_typed(sink, "permission_request", perm, level="urgent")
                except Exception as e:
                    logger.debug(f"Permission check error: {e}")

            # 3) Backlog candidate detection (no-op until re-enabled)
            candidate_check_counter += 1
            if candidate_check_counter >= 10 and not state.connection_closed:
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


async def desktop_activity_monitor(state: TerminalSessionState, app, deps) -> None:
    """Detect desktop-side typing in the tmux pane.

    Polls ``tmux capture-pane`` every ``DESKTOP_POLL_INTERVAL`` seconds.
    If the captured screen has changed AND no input arrived from this
    client recently, infer that someone else is typing into the pane
    (probably the user at their desktop). Push a ``device_state`` event
    so the mobile client can dim its own input UI / show a banner.

    Auto-clears ``desktop_active`` after ``DESKTOP_IDLE_TIMEOUT`` seconds
    to avoid getting stuck on if the desktop session goes idle.
    """
    sink = state.sink
    last_hash = 0
    desktop_active = False
    desktop_since = 0.0

    while app.state.active_client is sink and not state.connection_closed:
        await asyncio.sleep(DESKTOP_POLL_INTERVAL)
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

            stdout = await asyncio.get_event_loop().run_in_executor(None, _capture_desktop)
            current_hash = hash(stdout)
            if current_hash != last_hash:
                last_hash = current_hash
                time_since_ws = time.time() - app.state.last_ws_input_time
                if time_since_ws > DESKTOP_POLL_INTERVAL and not desktop_active:
                    desktop_active = True
                    desktop_since = time.time()
                    await deps.send_typed(sink, "device_state",
                                          {"desktop_active": True}, level="info")
                elif time_since_ws <= DESKTOP_POLL_INTERVAL and desktop_active:
                    desktop_active = False
                    await deps.send_typed(sink, "device_state",
                                          {"desktop_active": False}, level="info")
            if desktop_active and (time.time() - desktop_since) > DESKTOP_IDLE_TIMEOUT:
                desktop_active = False
                await deps.send_typed(sink, "device_state",
                                      {"desktop_active": False}, level="info")
        except Exception:
            pass
