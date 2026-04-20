"""Tests for PaneRingBuffer — pure storage semantics, no transport."""

import os
import random

import pytest

from mobile_terminal.pane_buffer import (
    PaneRingBuffer,
    decide_resume,
    drop_session_pane_buffers,
    get_or_create_pane_buffer,
    pane_key,
)


# ── Construction / invariants ───────────────────────────────────────────

def test_fresh_buffer_starts_at_zero():
    buf = PaneRingBuffer()
    assert buf.oldest_seq == 0
    assert buf.next_seq == 0
    assert buf.retained_bytes == 0


def test_max_bytes_must_be_positive():
    with pytest.raises(ValueError):
        PaneRingBuffer(max_bytes=0)
    with pytest.raises(ValueError):
        PaneRingBuffer(max_bytes=-1)


# ── since() contract: empty / exact-tail / off-by-one ───────────────────

def test_since_zero_on_empty_returns_empty_bytes():
    buf = PaneRingBuffer()
    assert buf.since(0) == b""


def test_since_at_tail_returns_empty_bytes():
    buf = PaneRingBuffer()
    buf.append(b"abc")
    assert buf.since(3) == b""


def test_since_one_before_tail_returns_last_byte():
    buf = PaneRingBuffer()
    buf.append(b"abc")
    assert buf.since(2) == b"c"


def test_since_zero_returns_full_content():
    buf = PaneRingBuffer()
    buf.append(b"abc")
    assert buf.since(0) == b"abc"


def test_since_past_tail_returns_none():
    buf = PaneRingBuffer()
    buf.append(b"abc")
    assert buf.since(4) is None
    assert buf.since(999) is None


def test_since_before_oldest_returns_none():
    buf = PaneRingBuffer(max_bytes=4)
    buf.append(b"abcd")
    buf.append(b"efgh")  # forces eviction of "abcd"
    assert buf.oldest_seq == 4
    assert buf.since(0) is None
    assert buf.since(3) is None
    assert buf.since(4) == b"efgh"


# ── append() ────────────────────────────────────────────────────────────

def test_append_returns_seq_range():
    buf = PaneRingBuffer()
    assert buf.append(b"abc") == (0, 3)
    assert buf.append(b"de") == (3, 5)
    assert buf.next_seq == 5


def test_append_empty_is_noop():
    buf = PaneRingBuffer()
    buf.append(b"abc")
    result = buf.append(b"")
    assert result == (3, 3)
    assert buf.next_seq == 3
    assert buf.retained_bytes == 3


# ── Rollover ────────────────────────────────────────────────────────────

def test_rollover_evicts_oldest_chunk():
    buf = PaneRingBuffer(max_bytes=10)
    buf.append(b"0123456789")  # exactly at cap
    assert buf.oldest_seq == 0
    assert buf.next_seq == 10
    buf.append(b"abc")  # over cap → evict first chunk
    assert buf.oldest_seq == 10
    assert buf.next_seq == 13
    assert buf.retained_bytes == 3
    assert buf.since(10) == b"abc"


def test_rollover_keeps_at_least_one_chunk_when_single_chunk_exceeds_cap():
    buf = PaneRingBuffer(max_bytes=5)
    buf.append(b"0123456789")  # 10 bytes, exceeds cap, but it's the only chunk
    assert buf.oldest_seq == 0
    assert buf.next_seq == 10
    assert buf.since(0) == b"0123456789"


def test_rollover_oversize_chunk_then_smaller_evicts_oversize():
    buf = PaneRingBuffer(max_bytes=5)
    buf.append(b"0123456789")  # retained even though oversize
    buf.append(b"x")            # now 2 chunks, evict the big one
    assert buf.oldest_seq == 10
    assert buf.next_seq == 11
    assert buf.retained_bytes == 1
    assert buf.since(10) == b"x"
    assert buf.since(0) is None


def test_partial_chunk_slicing_across_eviction_boundary():
    buf = PaneRingBuffer(max_bytes=6)
    buf.append(b"abc")   # seq 0..3
    buf.append(b"def")   # seq 3..6
    buf.append(b"ghi")   # seq 6..9 → evicts "abc"
    assert buf.oldest_seq == 3
    assert buf.next_seq == 9
    assert buf.since(3) == b"defghi"
    assert buf.since(4) == b"efghi"  # mid-chunk slice
    assert buf.since(6) == b"ghi"
    assert buf.since(8) == b"i"


# ── Byte-exact contiguity (the load-bearing invariant) ──────────────────

def test_random_chunks_concatenate_to_original_tail():
    rng = random.Random(42)
    buf = PaneRingBuffer(max_bytes=4096)
    full = bytearray()
    for _ in range(200):
        size = rng.randint(1, 64)
        chunk = os.urandom(size)
        buf.append(chunk)
        full.extend(chunk)
    # Slice from oldest_seq forward must match the corresponding tail of `full`.
    expected_tail = bytes(full[buf.oldest_seq:])
    assert buf.since(buf.oldest_seq) == expected_tail
    # Random interior probes must also match.
    for _ in range(50):
        seq = rng.randint(buf.oldest_seq, buf.next_seq)
        assert buf.since(seq) == bytes(full[seq:])


# ── Identity / no-reset rule ────────────────────────────────────────────

def test_no_reset_method_exists():
    """Respawn must construct a new buffer; reset() would invite seq reuse bugs."""
    assert not hasattr(PaneRingBuffer(), "reset")


def test_two_buffers_are_independent():
    a = PaneRingBuffer()
    b = PaneRingBuffer()
    a.append(b"hello")
    assert b.next_seq == 0
    assert b.since(0) == b""
    assert a.since(0) == b"hello"


# ── Registry helper ─────────────────────────────────────────────────────

def test_pane_key_handles_none():
    assert pane_key(None, None) == ":"
    assert pane_key("alpha", None) == "alpha:"
    assert pane_key(None, "2:0") == ":2:0"
    assert pane_key("alpha", "2:0") == "alpha:2:0"


def test_get_or_create_returns_same_buffer_for_same_key():
    reg = {}
    a = get_or_create_pane_buffer(reg, "s", "2:0")
    b = get_or_create_pane_buffer(reg, "s", "2:0")
    assert a is b
    a.append(b"hi")
    assert get_or_create_pane_buffer(reg, "s", "2:0").since(0) == b"hi"


def test_get_or_create_returns_different_buffers_for_different_keys():
    reg = {}
    a = get_or_create_pane_buffer(reg, "s", "2:0")
    b = get_or_create_pane_buffer(reg, "s", "2:1")
    c = get_or_create_pane_buffer(reg, "other", "2:0")
    assert a is not b
    assert a is not c
    assert b is not c
    a.append(b"AAA")
    b.append(b"BBB")
    assert a.since(0) == b"AAA"
    assert b.since(0) == b"BBB"
    assert c.since(0) == b""


def test_registry_clear_drops_all_buffers():
    """Mirrors the tmux-disconnect path: dict.clear() must invalidate
    every buffer so a new tmux session can't inherit old pane bytes."""
    reg = {}
    get_or_create_pane_buffer(reg, "s", "2:0").append(b"old")
    reg.clear()
    fresh = get_or_create_pane_buffer(reg, "s", "2:0")
    assert fresh.next_seq == 0
    assert fresh.since(0) == b""


# ── decide_resume() ─────────────────────────────────────────────────────

def test_decide_resume_fresh_connect_no_since():
    buf = PaneRingBuffer()
    buf.append(b"hello")
    d = decide_resume(buf, None)
    assert d.mode == "fresh"
    assert d.delta is None
    assert d.send_clear_screen is True
    assert d.baseline_seq == 5


def test_decide_resume_caught_up():
    buf = PaneRingBuffer()
    buf.append(b"hello")
    d = decide_resume(buf, 5)
    assert d.mode == "caught_up"
    assert d.delta == b""
    assert d.send_clear_screen is False
    assert d.baseline_seq == 5


def test_decide_resume_delta_in_window():
    buf = PaneRingBuffer()
    buf.append(b"hello world")  # seq 0..11
    d = decide_resume(buf, 6)
    assert d.mode == "delta"
    assert d.delta == b"world"
    assert d.send_clear_screen is False
    assert d.baseline_seq == 11


def test_decide_resume_since_too_old_falls_back_to_snapshot():
    buf = PaneRingBuffer(max_bytes=4)
    buf.append(b"abcd")
    buf.append(b"efgh")  # evicts "abcd"
    d = decide_resume(buf, 0)
    assert d.mode == "snapshot"
    assert d.delta is None
    assert d.send_clear_screen is True
    assert d.baseline_seq == 8


def test_decide_resume_since_past_tail_falls_back_to_snapshot():
    """Stale identity case: client claims seq beyond what server produced
    (e.g. server restarted). Treat as out of window so client gets a
    fresh start, not a misleading delta."""
    buf = PaneRingBuffer()
    buf.append(b"abc")
    d = decide_resume(buf, 999)
    assert d.mode == "snapshot"
    assert d.delta is None
    assert d.send_clear_screen is True
    assert d.baseline_seq == 3


def test_decide_resume_since_zero_on_empty_is_caught_up():
    buf = PaneRingBuffer()
    d = decide_resume(buf, 0)
    assert d.mode == "caught_up"
    assert d.delta == b""
    assert d.send_clear_screen is False
    assert d.baseline_seq == 0


def test_drop_session_pane_buffers_removes_only_matching_session():
    """PTY respawn invalidates every pane in the affected session but
    not panes in other sessions sharing the same registry."""
    reg = {}
    get_or_create_pane_buffer(reg, "alpha", "1:0").append(b"a-1")
    get_or_create_pane_buffer(reg, "alpha", "2:0").append(b"a-2")
    get_or_create_pane_buffer(reg, "beta",  "1:0").append(b"b-1")
    n = drop_session_pane_buffers(reg, "alpha")
    assert n == 2
    assert "alpha:1:0" not in reg and "alpha:2:0" not in reg
    assert "beta:1:0" in reg
    # Recreating the alpha buffer starts fresh.
    fresh = get_or_create_pane_buffer(reg, "alpha", "1:0")
    assert fresh.next_seq == 0


def test_drop_session_pane_buffers_returns_zero_when_no_match():
    reg = {}
    get_or_create_pane_buffer(reg, "alpha", "1:0").append(b"x")
    assert drop_session_pane_buffers(reg, "missing") == 0
    assert "alpha:1:0" in reg  # untouched


def test_get_or_create_respects_max_bytes_on_first_construction():
    reg = {}
    buf = get_or_create_pane_buffer(reg, "s", "2:0", max_bytes=8)
    buf.append(b"0123456789")  # exceeds cap, single chunk retained
    buf.append(b"x")             # now evict the big chunk
    assert buf.oldest_seq == 10
    assert buf.since(10) == b"x"
