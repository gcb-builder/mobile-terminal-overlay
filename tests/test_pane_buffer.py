"""Tests for PaneRingBuffer — pure storage semantics, no transport."""

import os
import random

import pytest

from mobile_terminal.pane_buffer import PaneRingBuffer


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
