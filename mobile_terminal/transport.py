"""Transport abstraction for terminal client connections.

Provides a unified ClientSink interface so the rest of the app
doesn't care whether the client is connected via WebSocket or SSE.

Phase 3 (multi-sink): SinkRegistry holds N sinks concurrently. Each
sink owns a bounded output queue + a writer task that drains it to
the underlying transport. The PTY reader and broadcast helpers enqueue
into every sink's queue so terminal frames + control messages flow
through ONE ordering point per sink (PTY-read order = registry-enqueue
order = per-sink writer order). Slow sinks get drop-oldest backpressure
that protects control frames over output frames; fast sinks are
unaffected.
"""

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import AsyncIterator, Iterator, Optional, Protocol, runtime_checkable

from starlette.websockets import WebSocket, WebSocketState

logger = logging.getLogger(__name__)


# ── Per-sink queue config ────────────────────────────────────────────────
# Bounded so a slow client can't OOM the server. Big enough to absorb a
# few seconds of typical PTY output (~50 frames/sec) before drop-oldest
# kicks in. Tune up if drops become frequent in practice.
SINK_QUEUE_MAX = 256

# Frame kinds. "control" = banner / mode_changed / repo_change /
# permission_auto / queue_update — anything that conveys app state and
# should be lossless. "output" = PTY bytes / tail snapshots — droppable
# under pressure (the terminal will catch up via the next frame).
FRAME_CONTROL = "control"
FRAME_OUTPUT = "output"


@runtime_checkable
class ClientSink(Protocol):
    """Protocol for pushing data to a connected client."""

    async def send_json(self, data: dict) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def transport_type(self) -> str: ...


class WebSocketSink:
    """ClientSink backed by a Starlette WebSocket.

    Phase 3.1: gained a per-sink bounded output queue and writer-task
    handle (managed by SinkRegistry). The queue holds tagged frames
    (kind, payload) so backpressure can prefer dropping output over
    control. The writer task lives in the registry, not here, so
    register/unregister can own lifecycle atomically.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        # Output mode for the *receiver*: "tail" (compact JSON updates) or
        # "full" (raw PTY bytes). Mirrors SSESink.client_mode so the
        # shared terminal_session runners can read it via ``sink.client_mode``
        # regardless of transport.
        self.client_mode: str = "tail"
        # Per-sink output pipeline (filled in by SinkRegistry.register).
        # Use a plain deque + condition rather than asyncio.Queue because
        # we need surgical drop-oldest-OUTPUT semantics that Queue doesn't
        # expose. The lock + Event give the writer something to await on.
        self.queue: deque = deque()
        self.queue_event: asyncio.Event = asyncio.Event()
        self.queue_lock: asyncio.Lock = asyncio.Lock()
        # Drop counters for diagnostics. drops_output is expected under
        # load; drops_control is a bug or runaway producer — log loudly.
        self.drops_output: int = 0
        self.drops_control: int = 0

    @property
    def is_connected(self) -> bool:
        return self._ws.client_state == WebSocketState.CONNECTED

    @property
    def transport_type(self) -> str:
        return "ws"

    @property
    def ws(self) -> WebSocket:
        """Access the underlying WebSocket (for receive loop)."""
        return self._ws

    async def send_json(self, data: dict) -> None:
        await self._ws.send_json(data)

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def send_text(self, text: str) -> None:
        await self._ws.send_text(text)

    async def close(self, code: int = 1000) -> None:
        try:
            await self._ws.close(code=code)
        except Exception:
            pass


# ── SinkRegistry ─────────────────────────────────────────────────────────

class SinkRegistry:
    """Holds the set of connected ClientSinks and owns their writer tasks.

    The registry is the single source of truth for "who is connected".
    Outbound paths (PTY reader, banner emitters, queue/backlog notifiers)
    call ``await registry.broadcast(kind, payload)`` rather than touching
    individual sinks. Sinks are added via ``await register(sink)`` and
    cleaned up via ``await unregister(sink)`` (typically in a try/finally
    in the transport handler).

    Phase 3.1: this scaffolding exists alongside the legacy
    ``app.state.active_client`` single-sink path. Migration happens in
    3.2 (outbound calls) and 3.3 (PTY reader). 3.4 drops the kicker.
    """

    def __init__(self) -> None:
        self._sinks: set[ClientSink] = set()
        self._writers: dict[int, asyncio.Task] = {}  # id(sink) -> writer task
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._sinks)

    def __iter__(self) -> Iterator[ClientSink]:
        # Snapshot to avoid mutation-during-iter; callers may unregister
        # mid-broadcast on send failure.
        return iter(tuple(self._sinks))

    def __contains__(self, sink: ClientSink) -> bool:
        return sink in self._sinks

    def latest(self) -> Optional[ClientSink]:
        """Return one sink (most recently registered) or None.

        Temporary helper for unmigrated code paths in 3.1/3.2. Prefer
        broadcast() once the call site is migrated. Callers using this
        WILL get logged via the active_client shim — the long-term path
        is to remove all latest() callers.
        """
        if not self._sinks:
            return None
        # Insertion order is not preserved by set; use the writer dict
        # which is insertion-ordered (Python 3.7+) — last entry = latest.
        if not self._writers:
            return next(iter(self._sinks), None)
        last_id = next(reversed(self._writers))
        for s in self._sinks:
            if id(s) == last_id:
                return s
        return next(iter(self._sinks), None)

    async def register(self, sink: ClientSink) -> None:
        """Add a sink and spawn its writer task atomically.

        After this returns, broadcast() will deliver to the sink and the
        writer will drain its queue to the underlying transport. Idempotent
        (re-register is a no-op) so reconnect races don't double-spawn.
        """
        async with self._lock:
            if sink in self._sinks:
                return
            self._sinks.add(sink)
            task = asyncio.create_task(
                _sink_writer(sink),
                name=f"sink_writer_{sink.transport_type}_{id(sink):x}",
            )
            self._writers[id(sink)] = task
        logger.info(
            f"[sink_registry] registered {sink.transport_type} "
            f"sink={id(sink):x} count={len(self._sinks)}"
        )

    async def unregister(self, sink: ClientSink) -> None:
        """Remove a sink and cancel its writer task.

        Safe to call multiple times (idempotent). Always wrap the
        register/use cycle in try/finally with this in the finally so
        zombie sinks can't accumulate after a network blip.
        """
        async with self._lock:
            if sink not in self._sinks:
                return
            self._sinks.discard(sink)
            task = self._writers.pop(id(sink), None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info(
            f"[sink_registry] unregistered {sink.transport_type} "
            f"sink={id(sink):x} count={len(self._sinks)} "
            f"drops_output={getattr(sink, 'drops_output', 0)} "
            f"drops_control={getattr(sink, 'drops_control', 0)}"
        )

    async def broadcast(self, kind: str, payload: object) -> None:
        """Enqueue a frame on every connected sink's queue.

        Frames are tagged with kind so per-sink backpressure can drop
        OUTPUT before CONTROL. Returns immediately — actual send
        happens in the per-sink writer task.

        kind: FRAME_CONTROL or FRAME_OUTPUT
        payload: dict (sent via send_json), bytes (send_bytes), or
                 str (send_text). Type determines transport call.
        """
        if kind not in (FRAME_CONTROL, FRAME_OUTPUT):
            logger.warning(f"[sink_registry] broadcast: unknown kind={kind!r}")
            return
        for sink in self:
            await _enqueue_frame(sink, kind, payload)


# ── Per-sink writer task + enqueue helper ────────────────────────────────

async def _enqueue_frame(sink: ClientSink, kind: str, payload: object) -> None:
    """Push a (kind, payload) frame onto sink.queue with overflow handling.

    Drop policy when full:
      1. Try to drop the oldest OUTPUT frame from the queue.
      2. If the queue is all CONTROL, drop the oldest CONTROL but log loudly
         (this is a runaway-producer signal).

    Either way the new frame goes in. Wakes the writer via queue_event.
    """
    q: deque = sink.queue
    async with sink.queue_lock:
        if len(q) >= SINK_QUEUE_MAX:
            # 1. Try to drop oldest OUTPUT
            dropped = False
            for i, (k, _) in enumerate(q):
                if k == FRAME_OUTPUT:
                    del q[i]
                    sink.drops_output = getattr(sink, "drops_output", 0) + 1
                    dropped = True
                    break
            # 2. Fallback: drop oldest CONTROL (rare; signals saturation)
            if not dropped:
                old_kind, _ = q.popleft()
                sink.drops_control = getattr(sink, "drops_control", 0) + 1
                logger.warning(
                    f"[sink_registry] {sink.transport_type} sink={id(sink):x} "
                    f"queue full of CONTROL frames, dropped oldest "
                    f"({old_kind}); slow client or runaway producer. "
                    f"drops_control={sink.drops_control}"
                )
        q.append((kind, payload))
        sink.queue_event.set()


async def _sink_writer(sink: ClientSink) -> None:
    """Drain sink.queue → underlying transport in PTY-read order.

    One writer per sink, spawned by SinkRegistry.register, cancelled by
    unregister. Catches transport errors and stops the loop (the outer
    transport handler's try/finally will unregister this sink).
    """
    try:
        while True:
            # Wait for a frame to be available
            await sink.queue_event.wait()
            # Drain everything currently queued (batches under load)
            async with sink.queue_lock:
                frames = list(sink.queue)
                sink.queue.clear()
                sink.queue_event.clear()
            for kind, payload in frames:
                if not sink.is_connected:
                    return
                # C1: tail-mode sinks don't want raw PTY bytes — the
                # tail_sender pushes compact JSON snapshots instead. The
                # central PTY reader broadcasts FRAME_OUTPUT to ALL
                # sinks; this filter keeps tail-mode sinks from getting
                # firehosed with raw bytes they'd just discard.
                if kind == FRAME_OUTPUT and getattr(sink, "client_mode", "tail") == "tail":
                    continue
                try:
                    if isinstance(payload, dict):
                        await sink.send_json(payload)
                    elif isinstance(payload, (bytes, bytearray)):
                        await sink.send_bytes(bytes(payload))
                    elif isinstance(payload, str):
                        await sink.send_text(payload)
                    else:
                        logger.warning(
                            f"[sink_writer] unexpected payload type "
                            f"{type(payload).__name__} kind={kind}"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Transport-level error (closed mid-send, etc.). Stop
                    # the writer; outer handler unregisters the sink.
                    logger.debug(
                        f"[sink_writer] {sink.transport_type} "
                        f"sink={id(sink):x} send failed: {e}"
                    )
                    return
    except asyncio.CancelledError:
        raise


# ── broadcast helpers ────────────────────────────────────────────────────

async def broadcast_typed(
    app, msg_type: str, payload: dict, level: str = "info"
) -> None:
    """Send a typed control message to every connected sink.

    Drop-in replacement for the legacy ``deps.send_typed(sink, ...)``
    pattern when the caller wants to fan out to ALL sinks instead of
    pushing to a single one. Frames are tagged FRAME_CONTROL so they're
    protected from output-frame backpressure.

    Wire format matches the legacy server.py:send_typed envelope:
    ``{"v": 2, "id", "type", "level", "session", "target", "ts",
    "payload"}``. The client only routes to handleTypedMessage when
    ``msg.v === 2`` (terminal.js:1637), so omitting the envelope drops
    the message into the legacy dispatcher silently — confirmed
    2026-04-26 when permission_auto toasts stopped appearing.
    """
    msg = {
        "v": 2,
        "id": str(uuid.uuid4()),
        "type": msg_type,
        "level": level,
        "session": getattr(app.state, "current_session", None),
        "target": getattr(app.state, "active_target", None),
        "ts": time.time(),
        "payload": payload,
    }
    await broadcast_raw(app, msg)


async def broadcast_raw(app, msg: dict) -> None:
    """Send a pre-formed dict to every connected sink, as-is.

    Use for legacy flat-shape messages whose schema is owned by the
    caller — e.g. backlog_update / queue_update / queue_sent which
    embed action+session+pane keys at the top level instead of inside
    a payload wrapper. Migrating those to the typed-wrapper format
    would require coordinated client+server changes; this helper
    preserves the existing wire format during the multi-sink migration.

    Tagged FRAME_CONTROL — same backpressure protection as broadcast_typed.
    """
    registry: Optional[SinkRegistry] = getattr(app.state, "sink_registry", None)
    if registry is None:
        logger.debug("[broadcast_raw] no sink_registry on app.state")
        return
    await registry.broadcast(FRAME_CONTROL, msg)
