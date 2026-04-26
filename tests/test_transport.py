"""Tests for transport.SinkRegistry + per-sink writer + broadcast helper.

Phase 3.1 scaffolding. Pins the contract before any call sites migrate:
  - register/unregister are idempotent and async-safe
  - broadcast fans out to every connected sink
  - per-sink bounded queue drops OUTPUT before CONTROL on overflow
  - writer task drains queue → transport in PTY-read order
  - sink lifecycle: writer task is born on register, killed on unregister

Uses asyncio.run() wrappers (no pytest-asyncio dependency) to match the
existing test suite's style.
"""

import asyncio
from collections import deque
from typing import Any

import pytest

from mobile_terminal.transport import (
    FRAME_CONTROL,
    FRAME_OUTPUT,
    SINK_QUEUE_MAX,
    SinkRegistry,
    broadcast_typed,
    _enqueue_frame,
)


def run(coro):
    """Run an async test body. Each test creates a fresh event loop so
    tasks/queues from one don't leak into the next."""
    return asyncio.run(coro)


class _FakeSink:
    """Minimal ClientSink stand-in. Records every send call in .sent."""

    def __init__(self, transport_type: str = "ws") -> None:
        self._transport_type = transport_type
        self._connected = True
        self.client_mode = "tail"
        # Per-sink output queue + writer prereqs (mirrors WebSocketSink).
        self.queue: deque = deque()
        self.queue_event = asyncio.Event()
        self.queue_lock = asyncio.Lock()
        self.drops_output = 0
        self.drops_control = 0
        # Test inspection
        self.sent: list[Any] = []
        # Optional knobs
        self.send_delay: float = 0.0
        self.fail_on_send: bool = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def transport_type(self) -> str:
        return self._transport_type

    async def send_json(self, data):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.fail_on_send:
            raise RuntimeError("simulated transport failure")
        self.sent.append(("json", data))

    async def send_bytes(self, data):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent.append(("bytes", data))

    async def send_text(self, text):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent.append(("text", text))

    async def close(self, code: int = 1000):
        self._connected = False


# ── Registry register/unregister ─────────────────────────────────────────


class TestRegister:
    def test_register_adds_sink(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            await r.register(s)
            assert len(r) == 1
            assert s in r
            await r.unregister(s)
        run(body())

    def test_register_idempotent(self):
        """Reconnect races shouldn't double-spawn writer tasks."""
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            await r.register(s)
            await r.register(s)  # second call no-ops
            assert len(r) == 1
            await r.unregister(s)
        run(body())

    def test_unregister_idempotent(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            await r.register(s)
            await r.unregister(s)
            await r.unregister(s)  # safe second call
            assert len(r) == 0
        run(body())

    def test_unregister_unknown_sink_safe(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            await r.unregister(s)  # never registered
            assert len(r) == 0
        run(body())

    def test_register_spawns_writer_task(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            await r.register(s)
            await _enqueue_frame(s, FRAME_CONTROL, {"type": "hello"})
            await asyncio.sleep(0.05)
            assert s.sent == [("json", {"type": "hello"})]
            await r.unregister(s)
        run(body())

    def test_unregister_cancels_writer(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            s.send_delay = 5.0  # would block forever if writer wasn't cancelled
            await r.register(s)
            await _enqueue_frame(s, FRAME_CONTROL, {"type": "hello"})
            # Don't await the send; immediately unregister.
            await asyncio.wait_for(r.unregister(s), timeout=1.0)
        run(body())

    def test_latest_returns_most_recent(self):
        """latest() is a temporary helper for unmigrated callers; should
        return the most-recently-registered sink so behavior matches the
        old "last connection wins" semantics."""
        async def body():
            r = SinkRegistry()
            s1 = _FakeSink("ws")
            s2 = _FakeSink("sse")
            await r.register(s1)
            await r.register(s2)
            assert r.latest() is s2
            await r.unregister(s2)
            assert r.latest() is s1
            await r.unregister(s1)
            assert r.latest() is None
        run(body())


# ── broadcast fan-out ────────────────────────────────────────────────────


class TestBroadcast:
    def test_broadcast_to_all_sinks(self):
        async def body():
            r = SinkRegistry()
            sinks = [_FakeSink() for _ in range(3)]
            for s in sinks:
                await r.register(s)
            await r.broadcast(FRAME_CONTROL, {"type": "perm_request", "id": "x"})
            await asyncio.sleep(0.05)
            for s in sinks:
                assert s.sent == [("json", {"type": "perm_request", "id": "x"})]
            for s in sinks:
                await r.unregister(s)
        run(body())

    def test_broadcast_with_no_sinks_is_noop(self):
        async def body():
            r = SinkRegistry()
            await r.broadcast(FRAME_CONTROL, {"x": 1})
        run(body())

    def test_broadcast_unknown_kind_is_noop(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            await r.register(s)
            await r.broadcast("bogus", {"x": 1})
            await asyncio.sleep(0.05)
            assert s.sent == []
            await r.unregister(s)
        run(body())

    def test_broadcast_typed_helper_wraps_message(self):
        """broadcast_typed wraps payload in the v=2 typed-message envelope
        — matches server.py:send_typed so the client's handleJsonMessage
        dispatcher (terminal.js:1637) routes it to handleTypedMessage.
        Without v:2 the message falls through to legacy handlers and
        type-routed handlers (permission_auto, permission_request,
        repo_change) never fire."""
        async def body():
            class _FakeApp:
                class state:
                    pass
            app = _FakeApp()
            app.state.current_session = "claude"
            app.state.active_target = "claude:1"
            r = SinkRegistry()
            app.state.sink_registry = r
            s = _FakeSink()
            await r.register(s)
            await broadcast_typed(app, "permission_auto", {"tool": "Bash"}, level="info")
            await asyncio.sleep(0.05)
            assert len(s.sent) == 1
            kind, msg = s.sent[0]
            assert kind == "json"
            assert msg["v"] == 2
            assert msg["type"] == "permission_auto"
            assert msg["level"] == "info"
            assert msg["session"] == "claude"
            assert msg["target"] == "claude:1"
            assert msg["payload"] == {"tool": "Bash"}
            assert isinstance(msg["id"], str) and len(msg["id"]) > 0
            assert isinstance(msg["ts"], float)
            await r.unregister(s)
        run(body())

    def test_broadcast_typed_no_registry_is_safe(self):
        """If app.state.sink_registry isn't set yet (early startup),
        broadcast_typed must not raise."""
        async def body():
            class _FakeApp:
                class state:
                    pass
            app = _FakeApp()
            await broadcast_typed(app, "anything", {"x": 1})
        run(body())

    def test_failing_sink_doesnt_break_others(self):
        """One slow/dead sink shouldn't poison the broadcast for others.
        The writer task on the failing sink stops; the registry retains
        the sink until the outer handler unregisters."""
        async def body():
            r = SinkRegistry()
            good = _FakeSink("ws")
            bad = _FakeSink("sse")
            bad.fail_on_send = True
            await r.register(good)
            await r.register(bad)
            await r.broadcast(FRAME_CONTROL, {"type": "ping"})
            await asyncio.sleep(0.05)
            assert good.sent == [("json", {"type": "ping"})]
            # bad's writer caught the exception and stopped; no crash
            await r.unregister(good)
            await r.unregister(bad)
        run(body())


# ── Backpressure / drop policy ───────────────────────────────────────────


class TestDropPolicy:
    def test_overflow_drops_output_before_control(self):
        """When the queue hits SINK_QUEUE_MAX, dropping policy must
        prefer OUTPUT frames so banners/permission_auto/etc survive."""
        async def body():
            s = _FakeSink()
            for i in range(SINK_QUEUE_MAX):
                kind = FRAME_OUTPUT if i % 2 == 0 else FRAME_CONTROL
                await _enqueue_frame(s, kind, f"frame-{i}")
            before_control = sum(1 for k, _ in s.queue if k == FRAME_CONTROL)
            before_output = sum(1 for k, _ in s.queue if k == FRAME_OUTPUT)
            await _enqueue_frame(s, FRAME_CONTROL, "new-control")
            after_control = sum(1 for k, _ in s.queue if k == FRAME_CONTROL)
            after_output = sum(1 for k, _ in s.queue if k == FRAME_OUTPUT)
            assert after_output == before_output - 1
            assert after_control == before_control + 1
            assert s.drops_output == 1
            assert s.drops_control == 0
        run(body())

    def test_all_control_overflow_drops_oldest_control_with_warning(self):
        """If the queue is somehow ALL control frames (runaway producer),
        drop oldest control rather than reject the new one. drops_control
        increments — that's the canary the operator watches for."""
        async def body():
            s = _FakeSink()
            for i in range(SINK_QUEUE_MAX):
                await _enqueue_frame(s, FRAME_CONTROL, f"c-{i}")
            await _enqueue_frame(s, FRAME_CONTROL, "newest")
            assert len(s.queue) == SINK_QUEUE_MAX
            assert s.drops_control == 1
            assert s.drops_output == 0
            kinds_payloads = list(s.queue)
            assert ("control", "c-0") not in kinds_payloads
            assert ("control", "newest") in kinds_payloads
        run(body())

    def test_under_limit_no_drops(self):
        async def body():
            s = _FakeSink()
            for i in range(SINK_QUEUE_MAX - 1):
                await _enqueue_frame(s, FRAME_OUTPUT, f"f-{i}")
            assert s.drops_output == 0
            assert s.drops_control == 0
            assert len(s.queue) == SINK_QUEUE_MAX - 1
        run(body())


# ── Writer ordering ──────────────────────────────────────────────────────


class TestWriterOrdering:
    def test_writer_preserves_enqueue_order(self):
        """PTY-read order must equal sink-receive order, otherwise the
        client sees scrambled bytes and a banner can land before the
        terminal frame that triggered it. Sink must be in 'full' mode
        for FRAME_OUTPUT to be delivered (C1 mode-filter)."""
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            s.client_mode = "full"  # accept FRAME_OUTPUT
            await r.register(s)
            for i in range(20):
                kind = FRAME_OUTPUT if i % 3 else FRAME_CONTROL
                await _enqueue_frame(s, kind, {"i": i})
            await asyncio.sleep(0.1)
            await r.unregister(s)
            ns = [data["i"] for (kind, data) in s.sent]
            assert ns == list(range(20))
        run(body())

    def test_writer_handles_bytes_and_text_payloads(self):
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            s.client_mode = "full"  # accept FRAME_OUTPUT bytes/text
            await r.register(s)
            await _enqueue_frame(s, FRAME_OUTPUT, b"raw pty bytes")
            await _enqueue_frame(s, FRAME_OUTPUT, "tail snapshot text")
            await _enqueue_frame(s, FRAME_CONTROL, {"type": "ping"})
            await asyncio.sleep(0.05)
            assert s.sent == [
                ("bytes", b"raw pty bytes"),
                ("text", "tail snapshot text"),
                ("json", {"type": "ping"}),
            ]
            await r.unregister(s)
        run(body())

    def test_tail_mode_sink_drops_frame_output(self):
        """C1: FRAME_OUTPUT must NOT be delivered to tail-mode sinks.
        The central PTY reader broadcasts to all sinks; the writer
        filter keeps tail-mode sinks from getting raw bytes they'd
        just discard. Tail snapshots come via FRAME_CONTROL."""
        async def body():
            r = SinkRegistry()
            s = _FakeSink()
            s.client_mode = "tail"  # default but explicit
            await r.register(s)
            await _enqueue_frame(s, FRAME_OUTPUT, b"raw pty bytes")
            await _enqueue_frame(s, FRAME_OUTPUT, "tail snapshot text")
            await _enqueue_frame(s, FRAME_CONTROL, {"type": "ping"})
            await asyncio.sleep(0.05)
            # Only the FRAME_CONTROL passes; both OUTPUT frames dropped.
            assert s.sent == [("json", {"type": "ping"})]
            await r.unregister(s)
        run(body())
