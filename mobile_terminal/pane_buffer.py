"""Per-pane ring buffer of raw PTY bytes for delta-based reconnect.

The buffer assigns each emitted byte a monotonically increasing absolute
``seq``. A reconnecting client says ``since=N`` and the server replies
with the bytes in ``(N, next_seq]`` instead of capturing and shipping
the full pane history.

Identity rule: a buffer's seq space is *meaningful only within its own
lifetime*. On PTY respawn the caller MUST discard the old buffer and
construct a new one — never reuse seqs across stream identities. There
is intentionally no ``reset()`` method to make this easy to misuse.

Rollover contract: if a client's ``since`` is older than the retained
window (``< oldest_seq``) or newer than the buffer has produced
(``> next_seq``), ``since()`` returns ``None`` and the caller falls
back to a full snapshot. No partial recovery — simpler is safer.
"""

from collections import deque
from dataclasses import dataclass
from typing import Optional


DEFAULT_MAX_BYTES = 1_048_576  # 1 MiB per pane


@dataclass(frozen=True)
class ResumeDecision:
    """Result of deciding how to handle a (re)connect.

    - ``baseline_seq``: what the server's pane buffer is at right now;
      what the client's lastSeq should be once the resume frames are
      processed.
    - ``delta``: bytes to ship as catch-up. ``None`` means "no delta
      send" (either fresh connect or out-of-window). ``b""`` means
      client is exactly caught up — no bytes to send but also no
      snapshot needed.
    - ``send_clear_screen``: whether to emit ``\\x1b[2J\\x1b[H``. False
      whenever a non-empty delta is being shipped, since clearing would
      wipe the xterm state the delta is meant to extend.
    - ``mode``: ``"fresh"`` | ``"snapshot"`` | ``"caught_up"`` | ``"delta"``.
      Diagnostic only; transport handlers don't branch on it.
    """
    baseline_seq: int
    delta: Optional[bytes]
    send_clear_screen: bool
    mode: str


def decide_resume(pbuf: "PaneRingBuffer", since: Optional[int]) -> ResumeDecision:
    """Pure decision: what to ship a (re)connecting client.

    - ``since is None`` → fresh connect: no delta, clear screen.
    - ``since`` outside buffer window → snapshot fallback: no delta,
      clear screen. (Caller still has to ship a snapshot if it wants
      one — this function only governs the delta wire.)
    - ``since == next_seq`` → caught up: no delta, no clear.
    - ``since`` in window → delta: ship the bytes, do NOT clear.
    """
    baseline = pbuf.next_seq
    if since is None:
        return ResumeDecision(baseline, None, True, "fresh")
    delta = pbuf.since(since)
    if delta is None:
        return ResumeDecision(baseline, None, True, "snapshot")
    if delta == b"":
        return ResumeDecision(baseline, b"", False, "caught_up")
    return ResumeDecision(baseline, delta, False, "delta")


def pane_key(session: Optional[str], target: Optional[str]) -> str:
    """Stable registry key for a pane. ``target`` may be None for the
    session's default pane (no explicit target selected)."""
    return f"{session or ''}:{target or ''}"


def get_or_create_pane_buffer(
    registry: dict, session: Optional[str], target: Optional[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> "PaneRingBuffer":
    """Look up the buffer for a pane, constructing it on first access.

    The registry is ``app.state.pane_buffers`` (a plain dict). Callers
    on the tmux-disconnect path MUST clear the registry so stale pane
    identities don't collide with a new tmux session's panes.
    """
    key = pane_key(session, target)
    buf = registry.get(key)
    if buf is None:
        buf = PaneRingBuffer(max_bytes=max_bytes)
        registry[key] = buf
    return buf


class PaneRingBuffer:
    """Bounded byte ring with absolute-seq addressing.

    Public API thinks in absolute byte positions. ``oldest_seq`` is the
    seq of the first retained byte; ``next_seq`` is the seq the next
    byte will receive (== one past the last retained byte). On a fresh
    buffer both are 0.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._chunks: "deque[tuple[int, bytes]]" = deque()  # (end_seq, data)
        self._oldest_seq: int = 0
        self._next_seq: int = 0
        self._total: int = 0
        self._max_bytes: int = max_bytes

    @property
    def oldest_seq(self) -> int:
        return self._oldest_seq

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def retained_bytes(self) -> int:
        return self._total

    def append(self, data: bytes) -> tuple[int, int]:
        """Append bytes and return ``(start_seq, end_seq)`` for the chunk.

        Empty input is a no-op returning ``(next_seq, next_seq)``.
        Eviction keeps total <= max_bytes but always retains at least
        one chunk, so the most recent data is always seekable even when
        a single chunk exceeds the cap.
        """
        if not data:
            return (self._next_seq, self._next_seq)
        start = self._next_seq
        self._next_seq += len(data)
        self._chunks.append((self._next_seq, data))
        self._total += len(data)
        while self._total > self._max_bytes and len(self._chunks) > 1:
            evicted_end, evicted = self._chunks.popleft()
            self._total -= len(evicted)
            self._oldest_seq = evicted_end
        return (start, self._next_seq)

    def since(self, seq: int) -> Optional[bytes]:
        """Return bytes in ``(seq, next_seq]`` or ``None`` if out of range.

        - ``seq < oldest_seq`` → ``None`` (rolled out, snapshot needed)
        - ``seq > next_seq``   → ``None`` (stale identity, snapshot needed)
        - ``seq == next_seq``  → ``b""`` (caller is up to date)
        - otherwise            → exact bytes from ``seq`` to ``next_seq``
        """
        if seq < self._oldest_seq or seq > self._next_seq:
            return None
        if seq == self._next_seq:
            return b""
        out = bytearray()
        for end, chunk in self._chunks:
            start = end - len(chunk)
            if end <= seq:
                continue
            if start < seq:
                out.extend(chunk[seq - start:])
            else:
                out.extend(chunk)
        return bytes(out)
