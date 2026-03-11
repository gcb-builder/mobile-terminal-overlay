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
