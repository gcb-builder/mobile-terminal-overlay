"""Content-addressable scratch storage for tool outputs."""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCRATCH_BASE = Path.home() / ".cache" / "mobile-overlay" / "scratch"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50MB


class ScratchStore:
    """Content-addressable file store for tool outputs.

    Storage layout:
        ~/.cache/mobile-overlay/scratch/{project_id}/{sha256}.json
        ~/.cache/mobile-overlay/scratch/{project_id}/_index.jsonl

    Each .json file contains the full entry (content_hash, tool_name,
    target, full_output, summary, timestamp, session_id, pane_id).
    The _index.jsonl is an append-only manifest for fast listing.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES):
        self._max_bytes = max_bytes

    def _project_dir(self, project_id: str) -> Path:
        d = SCRATCH_BASE / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "_index.jsonl"

    def _entry_path(self, project_id: str, content_hash: str) -> Path:
        return self._project_dir(project_id) / f"{content_hash}.json"

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def store(
        self,
        content: str,
        tool_name: str,
        target: str,
        summary: str,
        session_id: str,
        pane_id: str,
        project_id: str,
    ) -> Optional[dict]:
        """Store content. Returns index entry dict, or None on error.

        If content already exists (same hash), returns existing entry (dedup).
        """
        content_hash = self._hash_content(content)
        entry_path = self._entry_path(project_id, content_hash)

        # Dedup: if already stored, return existing index entry
        if entry_path.exists():
            try:
                existing = json.loads(entry_path.read_text())
                return self._make_index_entry(existing)
            except Exception:
                pass  # Re-store if corrupt

        now = time.time()
        entry = {
            "content_hash": content_hash,
            "tool_name": tool_name,
            "target": target,
            "full_output": content,
            "summary": summary,
            "timestamp": now,
            "session_id": session_id,
            "pane_id": pane_id,
        }

        try:
            entry_path.write_text(json.dumps(entry))
            size_bytes = entry_path.stat().st_size

            index_entry = {
                "content_hash": content_hash,
                "tool_name": tool_name,
                "target": target,
                "summary": summary,
                "timestamp": now,
                "session_id": session_id,
                "pane_id": pane_id,
                "size_bytes": size_bytes,
                "deleted": False,
            }
            with open(self._index_path(project_id), "a") as f:
                f.write(json.dumps(index_entry) + "\n")

            evicted = self._evict(project_id)
            index_entry["evicted"] = evicted
            return index_entry

        except OSError as e:
            logger.warning("ScratchStore.store failed: %s", e)
            return None

    def get(self, content_hash: str, project_id: str) -> Optional[dict]:
        """Get full entry by content hash."""
        entry_path = self._entry_path(project_id, content_hash)
        if not entry_path.exists():
            return None
        try:
            os.utime(entry_path, None)  # Touch atime for LRU
            return json.loads(entry_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("ScratchStore.get failed for %s: %s", content_hash, e)
            return None

    def get_summary(self, content_hash: str, project_id: str) -> Optional[dict]:
        """Get entry metadata without full_output."""
        entry = self.get(content_hash, project_id)
        if entry is None:
            return None
        return self._make_index_entry(entry)

    def list_entries(
        self, project_id: str, limit: int = 100, tool_name: Optional[str] = None
    ) -> List[dict]:
        """List entries from the index, newest first.

        Reads _index.jsonl, deduplicates (last write wins), filters deleted.
        """
        index_path = self._index_path(project_id)
        if not index_path.exists():
            return []

        entries: Dict[str, dict] = {}
        try:
            with open(index_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries[entry["content_hash"]] = entry
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            return []

        result = []
        for e in entries.values():
            if e.get("deleted"):
                continue
            if tool_name and e.get("tool_name") != tool_name:
                continue
            result.append(e)

        result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return result[:limit]

    def delete(self, content_hash: str, project_id: str) -> bool:
        """Delete an entry. Returns True if found and deleted."""
        entry_path = self._entry_path(project_id, content_hash)
        if not entry_path.exists():
            return False

        try:
            entry_path.unlink()
            with open(self._index_path(project_id), "a") as f:
                f.write(json.dumps({
                    "content_hash": content_hash,
                    "deleted": True,
                    "timestamp": time.time(),
                }) + "\n")
            return True
        except OSError as e:
            logger.warning("ScratchStore.delete failed for %s: %s", content_hash, e)
            return False

    def get_stats(self, project_id: str) -> dict:
        """Get storage statistics for a project."""
        project_dir = self._project_dir(project_id)
        total_bytes = 0
        entry_count = 0
        for f in project_dir.glob("*.json"):
            try:
                total_bytes += f.stat().st_size
                entry_count += 1
            except OSError:
                continue
        return {
            "entry_count": entry_count,
            "total_bytes": total_bytes,
            "max_bytes": self._max_bytes,
            "project_id": project_id,
        }

    def _evict(self, project_id: str) -> int:
        """LRU eviction by access time. Returns count evicted."""
        project_dir = self._project_dir(project_id)
        files = []
        total_bytes = 0

        for f in project_dir.glob("*.json"):
            try:
                stat = f.stat()
                files.append((f, stat.st_atime, stat.st_size))
                total_bytes += stat.st_size
            except OSError:
                continue

        if total_bytes <= self._max_bytes:
            return 0

        # Sort by access time ascending (oldest accessed first)
        files.sort(key=lambda x: x[1])

        evicted = 0
        for f, _atime, size in files:
            if total_bytes <= self._max_bytes:
                break
            try:
                content_hash = f.stem
                f.unlink()
                total_bytes -= size
                evicted += 1
                with open(self._index_path(project_id), "a") as idx:
                    idx.write(json.dumps({
                        "content_hash": content_hash,
                        "deleted": True,
                        "timestamp": time.time(),
                    }) + "\n")
            except OSError as e:
                logger.warning("ScratchStore eviction failed for %s: %s", f.name, e)
                continue

        if evicted:
            logger.info(
                "ScratchStore evicted %d entries for project %s", evicted, project_id
            )
        return evicted

    @staticmethod
    def _make_index_entry(full_entry: dict) -> dict:
        """Strip full_output from a full entry to make an index entry."""
        return {k: v for k, v in full_entry.items() if k != "full_output"}
