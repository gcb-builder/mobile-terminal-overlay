"""Tests for CommandQueue: enqueue, dequeue, policy, pause, ready-gate patterns."""
import re
import time

import pytest

from mobile_terminal.models import CommandQueue, QueueItem


# ---------------------------------------------------------------------------
# Enqueue / dequeue basics
# ---------------------------------------------------------------------------

class TestQueueBasics:
    def setup_method(self):
        self.q = CommandQueue()
        # Bypass disk load/save
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def test_enqueue_returns_new_item(self):
        item, is_new = self.q.enqueue("sess", "echo hello")
        assert is_new is True
        assert item.text == "echo hello"
        assert item.status == "queued"

    def test_enqueue_idempotent_by_id(self):
        item1, new1 = self.q.enqueue("sess", "echo hello", item_id="abc")
        item2, new2 = self.q.enqueue("sess", "echo hello", item_id="abc")
        assert new1 is True
        assert new2 is False
        assert item1.id == item2.id
        assert len(self.q.list_items("sess")) == 1

    def test_dequeue_removes_item(self):
        item, _ = self.q.enqueue("sess", "echo hello", item_id="x1")
        assert len(self.q.list_items("sess")) == 1
        removed = self.q.dequeue("sess", "x1")
        assert removed is True
        assert len(self.q.list_items("sess")) == 0

    def test_dequeue_missing_returns_false(self):
        self.q.enqueue("sess", "echo hello", item_id="x1")
        assert self.q.dequeue("sess", "nonexistent") is False
        assert len(self.q.list_items("sess")) == 1

    def test_dequeue_prevents_server_double_send(self):
        """After client-side drain calls dequeue, _process_loop won't find the item."""
        item, _ = self.q.enqueue("sess", "echo hello", item_id="drain1", policy="safe")
        self.q.dequeue("sess", "drain1")
        # Simulate what _process_loop does: find first queued safe item
        queue = self.q._get_queue("sess")
        found = None
        for i in queue:
            if i.status == "queued" and i.policy == "safe":
                found = i
                break
        assert found is None

    def test_list_items_returns_copy(self):
        self.q.enqueue("sess", "a")
        self.q.enqueue("sess", "b")
        items = self.q.list_items("sess")
        assert len(items) == 2
        items.clear()  # Mutating the copy shouldn't affect queue
        assert len(self.q.list_items("sess")) == 2

    def test_flush_clears_all(self):
        self.q.enqueue("sess", "a")
        self.q.enqueue("sess", "b")
        self.q.enqueue("sess", "c")
        count = self.q.flush("sess")
        assert count == 3
        assert len(self.q.list_items("sess")) == 0


# ---------------------------------------------------------------------------
# Per-pane scoping
# ---------------------------------------------------------------------------

class TestQueuePaneScoping:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def test_different_panes_are_isolated(self):
        self.q.enqueue("sess", "cmd1", pane_id="0:0")
        self.q.enqueue("sess", "cmd2", pane_id="1:0")
        assert len(self.q.list_items("sess", pane_id="0:0")) == 1
        assert len(self.q.list_items("sess", pane_id="1:0")) == 1
        assert self.q.list_items("sess", pane_id="0:0")[0].text == "cmd1"

    def test_dequeue_from_correct_pane(self):
        self.q.enqueue("sess", "cmd1", item_id="p1", pane_id="0:0")
        self.q.enqueue("sess", "cmd2", item_id="p2", pane_id="1:0")
        self.q.dequeue("sess", "p1", pane_id="0:0")
        assert len(self.q.list_items("sess", pane_id="0:0")) == 0
        assert len(self.q.list_items("sess", pane_id="1:0")) == 1


# ---------------------------------------------------------------------------
# Policy classification
# ---------------------------------------------------------------------------

class TestPolicyClassification:
    def setup_method(self):
        self.q = CommandQueue()

    def test_single_digit_is_safe(self):
        assert self.q._classify_policy("1") == "safe"
        assert self.q._classify_policy("9") == "safe"

    def test_yn_is_safe(self):
        assert self.q._classify_policy("y") == "safe"
        assert self.q._classify_policy("n") == "safe"

    def test_empty_is_safe(self):
        assert self.q._classify_policy("") == "safe"

    def test_pipe_is_unsafe(self):
        assert self.q._classify_policy("cat file | grep foo") == "unsafe"

    def test_sudo_is_unsafe(self):
        assert self.q._classify_policy("sudo rm -rf /") == "unsafe"

    def test_git_push_is_unsafe(self):
        assert self.q._classify_policy("git push origin main") == "unsafe"

    def test_short_command_is_safe(self):
        assert self.q._classify_policy("ls") == "safe"

    def test_long_command_is_unsafe(self):
        long_cmd = "a" * 51
        assert self.q._classify_policy(long_cmd) == "unsafe"

    def test_explicit_policy_overrides_auto(self):
        """When policy is explicitly 'safe' or 'unsafe', auto-classification is skipped."""
        item, _ = self.q.enqueue.__wrapped__(self.q, "sess", "sudo rm", policy="safe") if hasattr(self.q.enqueue, '__wrapped__') else (None, None)
        # Test via enqueue with explicit policy
        q = CommandQueue()
        q._loaded_sessions = set()
        q._load_from_disk = lambda *a: None
        q._save_to_disk = lambda *a: None
        item, _ = q.enqueue("sess", "sudo rm -rf /", policy="safe")
        assert item.policy == "safe"


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------

class TestQueuePauseResume:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def test_default_not_paused(self):
        assert self.q.is_paused("sess") is False

    def test_pause_and_resume(self):
        self.q.pause("sess")
        assert self.q.is_paused("sess") is True
        self.q.resume("sess")
        assert self.q.is_paused("sess") is False

    def test_pause_per_pane(self):
        self.q.pause("sess", pane_id="0:0")
        assert self.q.is_paused("sess", pane_id="0:0") is True
        assert self.q.is_paused("sess", pane_id="1:0") is False


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------

class TestQueueReorder:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def test_reorder_moves_item(self):
        self.q.enqueue("sess", "a", item_id="a1")
        self.q.enqueue("sess", "b", item_id="b1")
        self.q.enqueue("sess", "c", item_id="c1")

        # Move "a1" to index 2
        ok = self.q.reorder("sess", "a1", 2)
        assert ok is True
        texts = [i.text for i in self.q.list_items("sess")]
        assert texts == ["b", "c", "a"]

    def test_reorder_nonexistent_returns_false(self):
        self.q.enqueue("sess", "a", item_id="a1")
        assert self.q.reorder("sess", "nope", 0) is False


# ---------------------------------------------------------------------------
# Ready-gate patterns
# ---------------------------------------------------------------------------

class TestReadyGatePatterns:
    """Test PROMPT_PATTERNS and BUSY_PATTERNS used by _check_ready."""

    def test_prompt_patterns_match_shell_prompts(self):
        for pattern in CommandQueue.PROMPT_PATTERNS:
            regex = re.compile(pattern, re.MULTILINE)
            # Should match at line end
            if '❯' in pattern:
                assert regex.search("some output\n❯ ")
            elif r'\$' in pattern:
                assert regex.search("user@host:~$ ")
            elif r'#' in pattern:
                assert regex.search("root@host:~# ")
            elif '>>>' in pattern:
                assert regex.search(">>> ")

    def test_prompt_patterns_dont_match_mid_line(self):
        """Prompt patterns require end-of-line to avoid false positives."""
        for pattern in CommandQueue.PROMPT_PATTERNS:
            regex = re.compile(pattern, re.MULTILINE)
            # $, #, >>> mid-sentence should NOT match (they require trailing \s*$)
            assert not regex.search("the cost is $50 and counting")

    def test_busy_patterns_match_interactive_prompts(self):
        cases = [
            ("Do you want to continue? [y/n]", True),
            ("Do you want to continue? [Y/n]", True),
            ("Default no [y/N]", True),
            ("Do you want to proceed?", True),
            ("Allow edit to file.py", True),
            ("? (4 options)", True),
            ("? (1 option)", True),
        ]
        for text, should_match in cases:
            matched = any(
                re.search(p, text, re.MULTILINE)
                for p in CommandQueue.BUSY_PATTERNS
            )
            assert matched == should_match, f"Expected {should_match} for: {text!r}"

    def test_busy_patterns_block_before_prompt_patterns(self):
        """If both busy and prompt patterns match, busy should win (checked first)."""
        # Terminal output: prompt visible but also [y/n] visible
        content = "Allow edit? [y/n]\n❯ "
        busy_hit = any(
            re.search(p, content, re.MULTILINE)
            for p in CommandQueue.BUSY_PATTERNS
        )
        prompt_hit = any(
            re.search(p, content, re.MULTILINE)
            for p in CommandQueue.PROMPT_PATTERNS
        )
        assert busy_hit is True
        assert prompt_hit is True
        # In _check_ready, busy is checked first and returns False


# ---------------------------------------------------------------------------
# get_next_unsafe
# ---------------------------------------------------------------------------

class TestGetNextUnsafe:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def test_returns_first_unsafe_queued(self):
        self.q.enqueue("sess", "ls", policy="safe", item_id="s1")
        self.q.enqueue("sess", "sudo rm", policy="unsafe", item_id="u1")
        self.q.enqueue("sess", "cat | grep", policy="unsafe", item_id="u2")
        item = self.q.get_next_unsafe("sess")
        assert item is not None
        assert item.id == "u1"

    def test_returns_none_when_no_unsafe(self):
        self.q.enqueue("sess", "ls", policy="safe")
        assert self.q.get_next_unsafe("sess") is None

    def test_skips_sent_items(self):
        item, _ = self.q.enqueue("sess", "sudo rm", policy="unsafe", item_id="u1")
        item.status = "sent"
        assert self.q.get_next_unsafe("sess") is None


# ---------------------------------------------------------------------------
# Replay protection — re-enqueue of an already-sent id must not re-run.
# Regression guard for the case where a client missed the queue_sent WS
# broadcast (mobile reconnect) and reconcileQueue re-POSTed the item.
# ---------------------------------------------------------------------------

class TestReplayProtection:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def _mark_sent(self, session, item_id, ts=None):
        """Simulate what _send_item does on a successful drain."""
        key = self.q._queue_key(session)
        # Find the item in the queue, mark it sent, populate cache.
        for it in self.q._queues.get(key, []):
            if it.id == item_id:
                it.status = "sent"
                it.sent_at = ts if ts is not None else time.time()
                self.q._recently_sent.setdefault(key, {})[item_id] = (it, it.sent_at)
                return it
        raise AssertionError(f"item {item_id} not found")

    def test_re_enqueue_after_send_returns_existing_not_new(self):
        # First enqueue + simulated drain.
        item1, new1 = self.q.enqueue("sess", "echo hi", item_id="X1")
        assert new1 is True
        self._mark_sent("sess", "X1")

        # Item is gone from the active queue (drained + sent).
        # Simulate the client's reconcile re-enqueueing the same id.
        item2, new2 = self.q.enqueue("sess", "echo hi", item_id="X1")

        assert new2 is False, "second enqueue with same id must NOT be treated as new"
        assert item2.status == "sent", "must return the historical sent snapshot"
        # The item is NOT re-added to the active queue — agent must not re-run.
        active = [i for i in self.q.list_items("sess") if i.status == "queued"]
        assert all(i.id != "X1" for i in active), "sent id must not reappear as queued"

    def test_replay_cache_expires_after_ttl(self):
        # Simulate a server restart between drain and re-enqueue:
        #  - item was sent in a previous process
        #  - new process loaded only queued items from disk (sent items
        #    don't survive _load_from_disk filtering), so the in-memory
        #    queue is empty
        #  - the recently_sent cache entry is what's left, but it's
        #    older than the TTL so it should be pruned and ignored
        item1, _ = self.q.enqueue("sess", "echo hi", item_id="X2")
        old_ts = time.time() - (self.q.SENT_ID_TTL_SECONDS + 60)
        self._mark_sent("sess", "X2", ts=old_ts)
        # Drop from active queue so only the cache entry gates this id.
        self.q._queues[self.q._queue_key("sess")] = []

        # Re-enqueue. Cache entry is past TTL → pruned → fresh enqueue.
        item2, new2 = self.q.enqueue("sess", "echo hi", item_id="X2")
        assert new2 is True
        assert item2.status == "queued"

    def test_replay_cache_blocks_after_restart_within_ttl(self):
        # Same restart simulation as above, but with a fresh timestamp.
        # The cache entry should still be live → block the re-enqueue.
        item1, _ = self.q.enqueue("sess", "echo hi", item_id="X4")
        self._mark_sent("sess", "X4")
        self.q._queues[self.q._queue_key("sess")] = []  # rebuilt-on-restart

        item2, new2 = self.q.enqueue("sess", "echo hi", item_id="X4")
        assert new2 is False, "cache must replay-block within TTL even when queue is empty"
        assert item2.status == "sent"
        # And the item must NOT be re-added to the queue.
        active = [i for i in self.q.list_items("sess") if i.status == "queued"]
        assert all(i.id != "X4" for i in active)

    def test_replay_cache_survives_restart_via_disk(self, tmp_path, monkeypatch):
        # Most realistic scenario: server drains item, restarts (deploy.sh,
        # systemd respawn, crash recovery), client reconnects after a
        # network switch and reconcileQueue re-POSTs the same id. The
        # in-memory cache is gone; without disk persistence the agent
        # would re-execute. With disk persistence, the cache reloads
        # and the replay-block still fires.
        from mobile_terminal import models
        monkeypatch.setattr(models, "QUEUE_DIR", tmp_path)

        # First "process": enqueue, mark sent (which writes the disk
        # cache via the real _save_to_disk path).
        q1 = CommandQueue()
        q1.enqueue("sess", "echo hi", item_id="X9")
        # Simulate a real successful drain: status flip + cache populate
        # + disk save (the same sequence _send_item performs on success).
        key = q1._queue_key("sess")
        item = q1._queues[key][0]
        item.status = "sent"
        item.sent_at = time.time()
        q1._recently_sent.setdefault(key, {})[item.id] = (item, item.sent_at)
        q1._save_to_disk("sess")

        # "Restart": brand-new CommandQueue. _load_from_disk fires on
        # first _get_queue access — and it filters out sent items, so
        # the queue starts empty. The recently-sent cache loads from
        # the sidecar file.
        q2 = CommandQueue()
        active = q2.list_items("sess")  # triggers _load_from_disk
        assert all(i.id != "X9" for i in active), "sent item must NOT survive in active queue"

        # Now the client's reconcile re-POSTs the same id. Cache should
        # block it.
        item2, new2 = q2.enqueue("sess", "echo hi", item_id="X9")
        assert new2 is False, "cache must survive restart and replay-block the re-enqueue"
        assert item2.status == "sent"
        active = [i for i in q2.list_items("sess") if i.status == "queued"]
        assert all(i.id != "X9" for i in active)

    def test_replay_cache_scoped_per_pane(self):
        # Same id sent on pane A; enqueued fresh on pane B should NOT
        # be replay-blocked (different queue keys, different histories).
        item1, _ = self.q.enqueue("sess", "echo hi", item_id="X3", pane_id="0:0")
        # Mark sent on pane 0:0
        key_a = self.q._queue_key("sess", "0:0")
        item1.status = "sent"
        item1.sent_at = time.time()
        self.q._recently_sent.setdefault(key_a, {})[item1.id] = (item1, item1.sent_at)

        item2, new2 = self.q.enqueue("sess", "echo hi", item_id="X3", pane_id="1:0")
        assert new2 is True, "different pane must not inherit the other pane's sent history"
        assert item2.status == "queued"


# ---------------------------------------------------------------------------
# Sent-item pruning — sent items past SENT_VISIBLE_TTL_SECONDS are removed
# from the active queue so the visible "Previous" section + on-disk file
# stay bounded across long-running sessions and idle reconnects.
# ---------------------------------------------------------------------------

class TestSentItemPrune:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def _mark_sent(self, session, item_id, ts):
        key = self.q._queue_key(session)
        for it in self.q._queues.get(key, []):
            if it.id == item_id:
                it.status = "sent"
                it.sent_at = ts
                return it
        raise AssertionError(f"item {item_id} not found")

    def test_prune_drops_old_sent_items(self):
        self.q.enqueue("sess", "old", item_id="OLD")
        self._mark_sent("sess", "OLD", ts=time.time() - (self.q.SENT_VISIBLE_TTL_SECONDS + 60))
        pruned = self.q._prune_old_sent("sess")
        assert pruned is True
        assert self.q.list_items("sess") == []

    def test_prune_keeps_recent_sent_items(self):
        self.q.enqueue("sess", "recent", item_id="REC")
        self._mark_sent("sess", "REC", ts=time.time() - 10)
        pruned = self.q._prune_old_sent("sess")
        assert pruned is False
        items = self.q.list_items("sess")
        assert len(items) == 1
        assert items[0].id == "REC"

    def test_prune_keeps_queued_items_regardless_of_age(self):
        """Queued items have no sent_at — pruning must not touch them."""
        item, _ = self.q.enqueue("sess", "still pending", item_id="QUE")
        # Even with a stale sent_at-style timestamp set externally, status
        # 'queued' overrides — we only prune by status==sent.
        item.sent_at = time.time() - 99999
        pruned = self.q._prune_old_sent("sess")
        assert pruned is False
        assert any(i.id == "QUE" for i in self.q.list_items("sess"))

    def test_prune_mixed_keeps_recent_drops_old(self):
        self.q.enqueue("sess", "old", item_id="OLD")
        self.q.enqueue("sess", "recent", item_id="REC")
        self.q.enqueue("sess", "queued", item_id="QUE")  # stays queued
        self._mark_sent("sess", "OLD", ts=time.time() - (self.q.SENT_VISIBLE_TTL_SECONDS + 60))
        self._mark_sent("sess", "REC", ts=time.time() - 5)
        pruned = self.q._prune_old_sent("sess")
        assert pruned is True
        ids = {i.id for i in self.q.list_items("sess")}
        assert ids == {"REC", "QUE"}

    def test_list_items_invokes_prune(self):
        """list_items must self-prune so a long-idle reconnect doesn't
        replay ancient sent items in the Previous section."""
        self.q.enqueue("sess", "old", item_id="STALE")
        self._mark_sent("sess", "STALE", ts=time.time() - (self.q.SENT_VISIBLE_TTL_SECONDS + 60))
        items = self.q.list_items("sess")
        assert items == []

    def test_prune_does_not_affect_replay_cache(self):
        """Visible-prune and replay-protection are independent. After
        visible prune, re-enqueueing the pruned id must STILL be blocked
        by the recently_sent cache (replay TTL is 600s vs visible 300s)."""
        item, _ = self.q.enqueue("sess", "old", item_id="DUP")
        ts = time.time() - (self.q.SENT_VISIBLE_TTL_SECONDS + 60)
        self._mark_sent("sess", "DUP", ts=ts)
        # Mirror what _send_item does: populate the replay cache.
        key = self.q._queue_key("sess")
        self.q._recently_sent.setdefault(key, {})[item.id] = (item, ts)

        self.q._prune_old_sent("sess")
        assert self.q.list_items("sess") == []

        # Even though item was pruned from the visible queue, replay
        # cache (still inside its 600s TTL) blocks re-execution.
        item2, is_new = self.q.enqueue("sess", "old", item_id="DUP")
        assert is_new is False
        assert item2.status == "sent"


# ---------------------------------------------------------------------------
# Wakeup signaling — enqueue() and resume() flip the asyncio event so the
# processor loop runs immediately instead of waiting out its idle timeout.
# ---------------------------------------------------------------------------

class TestWakeup:
    def setup_method(self):
        self.q = CommandQueue()
        self.q._loaded_sessions = set()
        self.q._load_from_disk = lambda *a: None
        self.q._save_to_disk = lambda *a: None

    def test_wake_is_safe_before_loop_starts(self):
        # _wakeup_event is None until _process_loop binds it. _wake()
        # must silently no-op rather than raise — otherwise enqueue()
        # would crash whenever it ran outside an event loop (e.g.
        # during startup or in tests).
        assert self.q._wakeup_event is None
        self.q._wake()  # Must not raise.

    def test_enqueue_calls_wake(self):
        # Bind a fake event so we can observe the set() call without
        # spinning up a real asyncio loop.
        class FakeEvent:
            def __init__(self):
                self.set_called = False
            def set(self):
                self.set_called = True
        fake = FakeEvent()
        self.q._wakeup_event = fake
        self.q.enqueue("sess", "echo hi")
        assert fake.set_called is True

    def test_resume_calls_wake(self):
        class FakeEvent:
            def __init__(self):
                self.set_called = False
            def set(self):
                self.set_called = True
        fake = FakeEvent()
        self.q._wakeup_event = fake
        self.q.pause("sess")
        fake.set_called = False  # Reset after pause (which doesn't wake).
        self.q.resume("sess")
        assert fake.set_called is True, "resume must wake the processor"
