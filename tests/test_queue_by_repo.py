"""Tests for the queue → repo-keyed storage refactor.

Covers the pure helpers (no tmux, no app required):
- repo_key_for_cwd is stable for the same path
- get_queue_file with repo_key vs pane_id resolves to different paths
- migrate_pane_to_repo atomically renames all 3 sidecars
- migration is idempotent (second call no-op)

The CommandQueue-side behavior (cwd-resolution fallback, multi-pane
same repo) is exercised by the existing test_queue.py suite, which
runs without tmux and stubs _resolve_repo_key implicitly via the
sync helper returning None (no live tmux session named 'sess').
"""

import json
from pathlib import Path

import pytest

from mobile_terminal.models import (
    repo_key_for_cwd,
    get_queue_file,
    get_sent_ids_file,
    get_tomb_file,
    migrate_pane_to_repo,
)


def test_repo_key_for_cwd_is_stable():
    """Same (session, path) → same repo_key, regardless of call timing."""
    a = repo_key_for_cwd("claude", "/home/me/dev/foo")
    b = repo_key_for_cwd("claude", "/home/me/dev/foo")
    assert a == b
    assert a.startswith("claude__")


def test_repo_key_distinguishes_paths():
    a = repo_key_for_cwd("claude", "/home/me/dev/foo")
    b = repo_key_for_cwd("claude", "/home/me/dev/bar")
    assert a != b


def test_repo_key_distinguishes_sessions():
    a = repo_key_for_cwd("alpha", "/home/me/dev/foo")
    b = repo_key_for_cwd("beta", "/home/me/dev/foo")
    assert a != b


def test_get_queue_file_uses_repo_key_when_provided():
    """repo_key wins over pane_id."""
    f = get_queue_file("s", pane_id="3:0", repo_key="s__-home-me-dev-foo")
    assert f.name == "s__-home-me-dev-foo.jsonl"


def test_get_queue_file_falls_back_to_pane_id():
    f = get_queue_file("claude", pane_id="3:0")
    assert "3_0" in f.name
    assert f.name.endswith(".jsonl")


def test_migrate_pane_to_repo_renames_all_sidecars(tmp_path, monkeypatch):
    """All three sidecar files (.jsonl, .sent.jsonl, .tomb.jsonl)
    must move atomically as a group."""
    monkeypatch.setattr("mobile_terminal.models.QUEUE_DIR", tmp_path)

    # Seed pane-keyed files
    (tmp_path / "claude_3_0.jsonl").write_text('{"id":"a","text":"x"}\n')
    (tmp_path / "claude_3_0.sent.jsonl").write_text('{"id":"b","sent_at":1}\n')
    (tmp_path / "claude_3_0.tomb.jsonl").write_text('{"id":"c","removed_at":1}\n')

    repo_key = repo_key_for_cwd("claude", "/home/me/dev/foo")
    out = migrate_pane_to_repo("claude", "3:0", repo_key)

    assert len(out["moved"]) == 3
    assert out["errors"] == []
    # Old files gone
    assert not (tmp_path / "claude_3_0.jsonl").exists()
    assert not (tmp_path / "claude_3_0.sent.jsonl").exists()
    assert not (tmp_path / "claude_3_0.tomb.jsonl").exists()
    # New files present with original content
    new_q = tmp_path / f"{repo_key}.jsonl"
    assert new_q.exists()
    assert json.loads(new_q.read_text().strip())["text"] == "x"


def test_migrate_pane_to_repo_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("mobile_terminal.models.QUEUE_DIR", tmp_path)
    (tmp_path / "claude_3_0.jsonl").write_text('{"id":"a"}\n')
    repo_key = repo_key_for_cwd("claude", "/home/me/dev/foo")

    first = migrate_pane_to_repo("claude", "3:0", repo_key)
    assert "jsonl -> ..." not in str(first)  # first migrate moved files
    second = migrate_pane_to_repo("claude", "3:0", repo_key)
    # Second call: nothing to move (old gone, new exists)
    assert second["moved"] == []
    assert second["errors"] == []


def test_migrate_skips_when_new_file_is_newer(tmp_path, monkeypatch):
    """Both files exist and new is newer — leave new alone, treat old as
    an orphan from a successful prior migration."""
    monkeypatch.setattr("mobile_terminal.models.QUEUE_DIR", tmp_path)
    (tmp_path / "claude_3_0.jsonl").write_text('{"text":"old"}\n')
    repo_key = repo_key_for_cwd("claude", "/home/me/dev/foo")
    new_path = tmp_path / f"{repo_key}.jsonl"
    new_path.write_text('{"text":"new"}\n')
    # Force new to be newer
    import os, time
    now = time.time()
    os.utime(tmp_path / "claude_3_0.jsonl", (now - 100, now - 100))
    os.utime(new_path, (now, now))

    out = migrate_pane_to_repo("claude", "3:0", repo_key)
    assert "jsonl" in out["skipped"]
    # New file untouched
    assert json.loads(new_path.read_text().strip())["text"] == "new"
    # Old file still there (orphan, can be cleaned up later)
    assert (tmp_path / "claude_3_0.jsonl").exists()


def test_migrate_recovers_when_old_file_is_newer(tmp_path, monkeypatch):
    """Recovery path for the v=400-and-earlier _save_to_disk bug:
    saves went to the pane-keyed file while loads read repo-keyed,
    so the pane-keyed file accumulated changes that the repo-keyed
    file never received. When mtime says old > new, treat the
    pane-keyed file as authoritative and overwrite the repo-keyed
    copy. Items the user dequeued/sent then stop reappearing."""
    monkeypatch.setattr("mobile_terminal.models.QUEUE_DIR", tmp_path)
    (tmp_path / "claude_3_0.jsonl").write_text('{"text":"new authoritative"}\n')
    repo_key = repo_key_for_cwd("claude", "/home/me/dev/foo")
    new_path = tmp_path / f"{repo_key}.jsonl"
    new_path.write_text('{"text":"stale"}\n')
    # Force old (pane-keyed) to be NEWER than the repo-keyed file
    import os, time
    now = time.time()
    os.utime(new_path, (now - 100, now - 100))
    os.utime(tmp_path / "claude_3_0.jsonl", (now, now))

    out = migrate_pane_to_repo("claude", "3:0", repo_key)
    moved_jsonl = [m for m in out["moved"] if m.startswith("claude_3_0.jsonl ->")]
    assert len(moved_jsonl) == 1
    assert "recovered" in moved_jsonl[0]
    # repo-keyed file now holds the recovered (newer) content
    assert json.loads(new_path.read_text().strip())["text"] == "new authoritative"
    # Old pane-keyed file is gone (replaced via Path.replace())
    assert not (tmp_path / "claude_3_0.jsonl").exists()


def test_migrate_no_old_files_is_clean_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("mobile_terminal.models.QUEUE_DIR", tmp_path)
    repo_key = repo_key_for_cwd("claude", "/home/me/dev/foo")
    out = migrate_pane_to_repo("claude", "3:0", repo_key)
    assert out["moved"] == []
    assert out["errors"] == []
    assert set(out["skipped"]) == {"jsonl", "sent.jsonl", "tomb.jsonl"}
