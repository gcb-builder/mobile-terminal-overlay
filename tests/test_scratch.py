"""Tests for ScratchStore content-addressable storage."""

import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from mobile_terminal.scratch import ScratchStore


class TestScratchStore:
    """Test ScratchStore operations using a temp directory."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patcher = patch(
            "mobile_terminal.scratch.SCRATCH_BASE", Path(self._tmpdir)
        )
        self._patcher.start()
        self.store = ScratchStore(max_bytes=10 * 1024)  # 10KB for tests

    def teardown_method(self):
        self._patcher.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_store_and_get(self):
        result = self.store.store(
            content="hello world",
            tool_name="Bash",
            target="0:0",
            summary="OK 1L",
            session_id="sess1",
            pane_id="0:0",
            project_id="test-project",
        )
        assert result is not None
        assert "content_hash" in result
        assert result["tool_name"] == "Bash"
        assert result["summary"] == "OK 1L"
        assert "full_output" not in result  # index entry, not full

        entry = self.store.get(result["content_hash"], "test-project")
        assert entry is not None
        assert entry["full_output"] == "hello world"
        assert entry["tool_name"] == "Bash"

    def test_store_returns_size_bytes(self):
        result = self.store.store("data", "Bash", "", "", "", "", "proj")
        assert "size_bytes" in result
        assert result["size_bytes"] > 0

    def test_dedup(self):
        r1 = self.store.store("same content", "Bash", "", "", "", "", "proj")
        r2 = self.store.store("same content", "Read", "", "", "", "", "proj")
        assert r1["content_hash"] == r2["content_hash"]

    def test_get_summary_excludes_full_output(self):
        result = self.store.store("data", "Bash", "", "OK", "", "", "proj")
        summary = self.store.get_summary(result["content_hash"], "proj")
        assert summary is not None
        assert "full_output" not in summary
        assert summary["content_hash"] == result["content_hash"]
        assert summary["summary"] == "OK"

    def test_get_nonexistent(self):
        assert self.store.get("nonexistent_hash", "proj") is None

    def test_get_summary_nonexistent(self):
        assert self.store.get_summary("nonexistent_hash", "proj") is None

    def test_list_entries(self):
        self.store.store("aaa", "Bash", "", "s1", "", "", "proj")
        self.store.store("bbb", "Read", "", "s2", "", "", "proj")
        entries = self.store.list_entries("proj")
        assert len(entries) == 2

    def test_list_filter_by_tool_name(self):
        self.store.store("aaa", "Bash", "", "", "", "", "proj")
        self.store.store("bbb", "Read", "", "", "", "", "proj")
        self.store.store("ccc", "Bash", "", "", "", "", "proj")
        entries = self.store.list_entries("proj", tool_name="Bash")
        assert len(entries) == 2
        assert all(e["tool_name"] == "Bash" for e in entries)

    def test_list_order_newest_first(self):
        self.store.store("first", "Bash", "", "", "", "", "proj")
        time.sleep(0.01)
        self.store.store("second", "Bash", "", "", "", "", "proj")
        entries = self.store.list_entries("proj")
        assert len(entries) == 2
        assert entries[0]["timestamp"] > entries[1]["timestamp"]

    def test_list_with_limit(self):
        for i in range(5):
            self.store.store(f"item-{i}", "Bash", "", "", "", "", "proj")
        entries = self.store.list_entries("proj", limit=2)
        assert len(entries) == 2

    def test_list_empty_project(self):
        entries = self.store.list_entries("nonexistent-proj")
        assert entries == []

    def test_delete(self):
        result = self.store.store("data", "Bash", "", "", "", "", "proj")
        ch = result["content_hash"]
        assert self.store.delete(ch, "proj") is True
        assert self.store.get(ch, "proj") is None

    def test_delete_nonexistent(self):
        assert self.store.delete("fake_hash", "proj") is False

    def test_list_excludes_deleted(self):
        result = self.store.store("data", "Bash", "", "", "", "", "proj")
        self.store.delete(result["content_hash"], "proj")
        entries = self.store.list_entries("proj")
        assert len(entries) == 0

    def test_eviction(self):
        """Storing more than max_bytes triggers LRU eviction."""
        for i in range(20):
            self.store.store(
                "x" * 600 + str(i),  # ~600 bytes content, unique
                "Bash", "", "", "", "", "proj",
            )
        stats = self.store.get_stats("proj")
        assert stats["total_bytes"] <= self.store._max_bytes

    def test_get_stats(self):
        self.store.store("data", "Bash", "", "", "", "", "proj")
        stats = self.store.get_stats("proj")
        assert stats["entry_count"] == 1
        assert stats["total_bytes"] > 0
        assert stats["max_bytes"] == 10 * 1024
        assert stats["project_id"] == "proj"

    def test_get_stats_empty(self):
        stats = self.store.get_stats("empty-proj")
        assert stats["entry_count"] == 0
        assert stats["total_bytes"] == 0

    def test_project_isolation(self):
        self.store.store("aaa", "Bash", "", "", "", "", "proj-a")
        self.store.store("bbb", "Bash", "", "", "", "", "proj-b")
        assert len(self.store.list_entries("proj-a")) == 1
        assert len(self.store.list_entries("proj-b")) == 1
        # Can't get proj-a's entry from proj-b
        r = self.store.store("aaa", "Bash", "", "", "", "", "proj-a")
        assert self.store.get(r["content_hash"], "proj-b") is None

    def test_hash_deterministic(self):
        r1 = self.store.store("same", "Bash", "", "", "", "", "proj")
        r2 = self.store.store("same", "Read", "", "", "", "", "proj")
        assert r1["content_hash"] == r2["content_hash"]

    def test_index_written(self):
        self.store.store("data", "Bash", "", "OK", "", "", "proj")
        index_path = Path(self._tmpdir) / "proj" / "_index.jsonl"
        assert index_path.exists()
        lines = index_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tool_name"] == "Bash"
        assert entry["deleted"] is False


class TestScratchRoutes:
    """Verify scratch routes appear in the app."""

    def test_scratch_routes_registered(self):
        from mobile_terminal.config import Config
        from mobile_terminal.server import create_app

        app = create_app(Config(session_name="test", no_auth=True))
        paths = {r.path for r in app.routes}

        assert "/api/scratch/list" in paths
        assert "/api/scratch/stats" in paths
        assert "/api/scratch/store" in paths
        assert "/api/scratch/{content_hash}" in paths
        assert "/api/scratch/{content_hash}/summary" in paths
