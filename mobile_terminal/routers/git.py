"""Routes for git operations: status, commits, revert, stash, discard, commit, push."""
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import run_subprocess

logger = logging.getLogger(__name__)

_pr_info_cache: Dict[str, dict] = {}  # repo_path -> {data, time}
PR_INFO_CACHE_TTL = 120.0  # seconds


def register(app: FastAPI, deps):
    """Register git routes."""

    @app.get("/api/rollback/git/status")
    async def git_status(
        _auth=Depends(deps.verify_token),
        pane_id: Optional[str] = Query(None),
    ):
        """Get current git status: branch, dirty, ahead/behind, lock status."""

        # If pane_id provided, verify it matches the active target
        if pane_id and app.state.active_target and pane_id != app.state.active_target:
            return {"has_repo": False, "error": "Pane mismatch"}

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return {
                "has_repo": False,
                "error": "No git repository found"
            }

        try:
            # Get current branch
            branch_result = await run_subprocess(
                ["git", "branch", "--show-current"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

            # Get dirty status
            status_result = await run_subprocess(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            status_lines = [l for l in status_result.stdout.strip().split('\n') if l]
            is_dirty = bool(status_lines)
            # Count untracked separately (lines starting with ??)
            untracked_files = sum(1 for l in status_lines if l.startswith('??'))
            dirty_files = len(status_lines) - untracked_files  # Modified/staged files

            # Get ahead/behind (may fail if no upstream)
            ahead = 0
            behind = 0
            has_upstream = False
            try:
                rev_result = await run_subprocess(
                    ["git", "rev-list", "--left-right", "--count", f"{branch}@{{upstream}}...HEAD"],
                    cwd=repo_path, capture_output=True, text=True, timeout=5
                )
                if rev_result.returncode == 0:
                    parts = rev_result.stdout.strip().split()
                    if len(parts) == 2:
                        behind = int(parts[0])
                        ahead = int(parts[1])
                        has_upstream = True
            except Exception:
                pass

            # Get repo path (relative to home for display)
            display_path = str(repo_path)
            home = str(Path.home())
            if display_path.startswith(home):
                display_path = "~" + display_path[len(home):]

            # Check for associated PR (cached, 120s TTL)
            pr_info = None
            cache_key = str(repo_path)
            cached_pr = _pr_info_cache.get(cache_key)
            if cached_pr and (time.time() - cached_pr["time"]) < PR_INFO_CACHE_TTL:
                pr_info = cached_pr["data"]
            else:
                try:
                    pr_result = await run_subprocess(
                        ["gh", "pr", "view", "--json", "number,title,url,state"],
                        cwd=repo_path, capture_output=True, text=True, timeout=5
                    )
                    if pr_result.returncode == 0:
                        pr_data = json.loads(pr_result.stdout)
                        pr_info = {
                            "number": pr_data.get("number"),
                            "title": pr_data.get("title"),
                            "url": pr_data.get("url"),
                            "state": pr_data.get("state"),
                        }
                    _pr_info_cache[cache_key] = {"data": pr_info, "time": time.time()}
                except Exception:
                    pass  # gh not available or no PR

            return {
                "has_repo": True,
                "repo_path": display_path,
                "branch": branch,
                "is_dirty": is_dirty,
                "dirty_files": dirty_files,
                "untracked_files": untracked_files,
                "has_upstream": has_upstream,
                "ahead": ahead,
                "behind": behind,
                "op_locked": app.state.git_op_lock.is_locked,
                "current_op": app.state.git_op_lock.current_operation,
                "pr": pr_info,
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/rollback/git/commits")
    async def list_git_commits(
        limit: int = Query(20),
        _auth=Depends(deps.verify_token),
    ):
        """List recent git commits."""

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            result = await run_subprocess(
                ["git", "log", f"--max-count={limit}", "--format=%H|%s|%an|%ad", "--date=short"],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return JSONResponse({"error": result.stderr}, status_code=500)

            commits = []
            for line in result.stdout.strip().split("\n"):
                if "|" in line:
                    parts = line.split("|", 3)
                    commits.append({
                        "hash": parts[0],
                        "subject": parts[1],
                        "author": parts[2] if len(parts) > 2 else "",
                        "date": parts[3] if len(parts) > 3 else ""
                    })

            return {"commits": commits}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/history")
    async def get_unified_history(
        limit: int = Query(30),
        _auth=Depends(deps.verify_token),
    ):
        """Get unified history: commits + snapshots merged chronologically."""

        items = []
        session = app.state.current_session

        # Get commits with unix timestamps
        repo_path = deps.get_current_repo_path()
        if repo_path:
            try:
                result = await run_subprocess(
                    ["git", "log", f"--max-count={limit}", "--format=%H|%s|%an|%at"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if "|" in line:
                            parts = line.split("|", 3)
                            ts = int(parts[3]) * 1000 if len(parts) > 3 else 0
                            items.append({
                                "type": "commit",
                                "id": parts[0][:7],
                                "hash": parts[0],
                                "subject": parts[1],
                                "author": parts[2] if len(parts) > 2 else "",
                                "timestamp": ts,
                            })
            except Exception as e:
                logger.warning(f"Failed to get commits for history: {e}")

        # Get snapshots (try target-scoped first, then session)
        target = app.state.active_target
        snap_key = f"{session}:{target}" if target else session
        snapshots = app.state.snapshot_buffer.list_snapshots(snap_key, limit)
        if not snapshots and target:
            snapshots = app.state.snapshot_buffer.list_snapshots(session, limit)
        for snap in snapshots:
            items.append({
                "type": "snapshot",
                "id": snap["id"],
                "label": snap["label"],
                "timestamp": snap["timestamp"],
                "pinned": snap.get("pinned", False),
                "pane_id": snap.get("pane_id", ""),
                "note": snap.get("note", ""),
                "image_path": snap.get("image_path"),
                "git_head": snap.get("git_head", ""),
            })

        # Sort by timestamp descending (newest first)
        items.sort(key=lambda x: x["timestamp"], reverse=True)

        # Limit total
        items = items[:limit]

        return {"items": items, "session": session}

    @app.get("/api/rollback/git/commit/{commit_hash}")
    async def get_git_commit_detail(
        commit_hash: str,
        _auth=Depends(deps.verify_token),
    ):
        """Get commit details including changed files."""

        # Validate hash format (security)
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            result = await run_subprocess(
                ["git", "show", "--stat", "--format=%H%n%s%n%b%n---AUTHOR---%n%an%n---DATE---%n%ad", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return JSONResponse({"error": "Commit not found"}, status_code=404)

            output = result.stdout
            parts = output.split("\n---AUTHOR---\n")
            first_part = parts[0].split("\n", 2)
            hash_val = first_part[0] if len(first_part) > 0 else commit_hash
            subject = first_part[1] if len(first_part) > 1 else ""
            body = first_part[2] if len(first_part) > 2 else ""

            author_part = parts[1].split("\n---DATE---\n") if len(parts) > 1 else ["", ""]
            author = author_part[0] if len(author_part) > 0 else ""
            rest = author_part[1] if len(author_part) > 1 else ""
            date_and_stat = rest.split("\n", 1)
            date = date_and_stat[0] if len(date_and_stat) > 0 else ""
            stat = date_and_stat[1] if len(date_and_stat) > 1 else ""

            return {
                "hash": hash_val,
                "subject": subject,
                "body": body.strip(),
                "author": author,
                "date": date,
                "stat": stat.strip()
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/rollback/git/revert/dry-run")
    async def dry_run_revert(
        commit_hash: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """Preview revert without executing."""

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("dry_run"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Check for uncommitted changes
            status = await run_subprocess(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if status.stdout.strip():
                return JSONResponse({
                    "error": "Working directory not clean",
                    "details": "Commit or stash changes first"
                }, status_code=400)

            # Try revert with --no-commit to preview
            result = await run_subprocess(
                ["git", "revert", "--no-commit", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                # Reset any partial changes
                await run_subprocess(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                               capture_output=True, timeout=10)
                return {
                    "success": False,
                    "error": "Revert would fail",
                    "details": result.stderr
                }

            # Get what would change
            diff = await run_subprocess(
                ["git", "diff", "--cached", "--stat"],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )

            # Reset the staged revert
            await run_subprocess(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                           capture_output=True, timeout=10)

            app.state.audit_log.log("revert_dry_run", {"commit": commit_hash})

            return {
                "success": True,
                "commit": commit_hash,
                "changes": diff.stdout,
                "message": f"Revert \"{commit_hash[:7]}\" would succeed"
            }
        except subprocess.TimeoutExpired:
            # Clean up on timeout
            await run_subprocess(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                           capture_output=True, timeout=10)
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            await run_subprocess(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                           capture_output=True, timeout=10)
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/rollback/git/revert/execute")
    async def execute_revert(
        commit_hash: str = Query(...),
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Execute git revert."""

        # Validate target before destructive operation
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("execute_revert"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Check working directory is clean
            status = await run_subprocess(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if status.stdout.strip():
                return JSONResponse({
                    "error": "Working directory not clean"
                }, status_code=400)

            # Get current HEAD for undo
            head_before = (await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )).stdout.strip()

            # Execute revert
            result = await run_subprocess(
                ["git", "revert", "--no-edit", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "success": False,
                    "error": result.stderr
                }, status_code=500)

            # Get new HEAD
            new_head = (await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )).stdout.strip()

            app.state.audit_log.log("revert_execute", {
                "commit": commit_hash,
                "head_before": head_before,
                "head_after": new_head
            })

            return {
                "success": True,
                "reverted_commit": commit_hash,
                "new_commit": new_head,
                "undo_target": head_before
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/rollback/git/revert/undo")
    async def undo_revert(
        revert_commit: str = Query(..., description="SHA of the revert commit to undo"),
        _auth=Depends(deps.verify_token),
    ):
        """Undo a revert by reverting the revert commit (non-destructive)."""

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', revert_commit):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("undo_revert"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Check working directory is clean
            status = await run_subprocess(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if status.stdout.strip():
                return JSONResponse({
                    "error": "Working directory not clean"
                }, status_code=400)

            # Revert the revert commit (non-destructive, creates new commit)
            result = await run_subprocess(
                ["git", "revert", "--no-edit", revert_commit],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "success": False,
                    "error": "Undo failed - revert may have conflicts",
                    "details": result.stderr
                }, status_code=500)

            # Get new HEAD (the undo commit)
            new_head = (await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )).stdout.strip()

            app.state.audit_log.log("revert_undo", {
                "revert_commit": revert_commit,
                "undo_commit": new_head
            })

            return {
                "success": True,
                "reverted_commit": revert_commit,
                "new_commit": new_head
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    # ========== Git Stash API ==========

    @app.post("/api/git/stash/push")
    async def stash_push(
        _auth=Depends(deps.verify_token),
    ):
        """Create a stash with auto-generated message."""

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("stash_push"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            timestamp = int(time.time())
            message = f"mobile-overlay-auto-stash-{timestamp}"

            # Stash including untracked files
            result = await run_subprocess(
                ["git", "stash", "push", "-u", "-m", message],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "error": "Stash failed",
                    "details": result.stderr
                }, status_code=500)

            # Check if anything was actually stashed
            if "No local changes to save" in result.stdout:
                return JSONResponse({
                    "error": "No changes to stash"
                }, status_code=400)

            app.state.audit_log.log("stash_push", {"message": message})

            return {
                "success": True,
                "stash_ref": "stash@{0}",
                "message": message
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.get("/api/git/stash/list")
    async def stash_list(
        _auth=Depends(deps.verify_token),
    ):
        """List all stashes."""

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            result = await run_subprocess(
                ["git", "stash", "list", "--format=%gd|%s|%ar"],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )

            stashes = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) >= 2:
                    stashes.append({
                        "ref": parts[0],
                        "message": parts[1],
                        "date": parts[2] if len(parts) > 2 else ""
                    })

            return {"stashes": stashes}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/git/stash/apply")
    async def stash_apply(
        ref: str = Query("stash@{0}"),
        _auth=Depends(deps.verify_token),
    ):
        """Apply a stash without removing it."""

        # Validate stash ref format
        if not re.match(r'^stash@\{\d+\}$', ref):
            return JSONResponse({"error": "Invalid stash ref format"}, status_code=400)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("stash_apply"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            result = await run_subprocess(
                ["git", "stash", "apply", ref],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                # Check for conflicts
                if "CONFLICT" in result.stdout or "conflict" in result.stderr.lower():
                    return {
                        "success": False,
                        "conflict": True,
                        "details": result.stdout + result.stderr
                    }
                return JSONResponse({
                    "error": "Apply failed",
                    "details": result.stderr
                }, status_code=500)

            app.state.audit_log.log("stash_apply", {"ref": ref})

            return {
                "success": True,
                "ref": ref
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/git/stash/drop")
    async def stash_drop(
        ref: str = Query("stash@{0}"),
        _auth=Depends(deps.verify_token),
    ):
        """Drop a stash."""

        # Validate stash ref format
        if not re.match(r'^stash@\{\d+\}$', ref):
            return JSONResponse({"error": "Invalid stash ref format"}, status_code=400)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("stash_drop"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            result = await run_subprocess(
                ["git", "stash", "drop", ref],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )

            if result.returncode != 0:
                return JSONResponse({
                    "error": "Drop failed",
                    "details": result.stderr
                }, status_code=500)

            app.state.audit_log.log("stash_drop", {"ref": ref})

            return {"success": True, "ref": ref}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    # ========== Git Discard API ==========

    @app.post("/api/git/discard")
    async def git_discard(
        include_untracked: bool = Query(False),
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Discard all uncommitted changes. Optionally remove untracked files."""

        # Validate target before destructive operation
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("discard"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Get list of files that will be discarded (for logging)
            status_result = await run_subprocess(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            files_to_discard = [
                line[3:] for line in status_result.stdout.strip().split("\n")
                if line and not line.startswith("??")
            ]
            untracked_files = [
                line[3:] for line in status_result.stdout.strip().split("\n")
                if line and line.startswith("??")
            ]

            # Reset tracked files
            result = await run_subprocess(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "error": "Reset failed",
                    "details": result.stderr
                }, status_code=500)

            # Optionally clean untracked files
            cleaned_files = []
            if include_untracked and untracked_files:
                clean_result = await run_subprocess(
                    ["git", "clean", "-fd"],
                    cwd=repo_path, capture_output=True, text=True, timeout=30
                )
                if clean_result.returncode == 0:
                    cleaned_files = untracked_files

            app.state.audit_log.log("git_discard", {
                "files_reset": files_to_discard,
                "files_cleaned": cleaned_files,
                "include_untracked": include_untracked
            })

            return {
                "success": True,
                "files_discarded": files_to_discard,
                "files_cleaned": cleaned_files
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    # ========== Quick Git Actions (commit, push) ==========

    @app.post("/api/git/commit")
    async def git_commit(
        request: Request,
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Stage all changes and commit with a message."""

        # Validate target to prevent operating on wrong repo
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            body = await request.json()
            message = body.get("message", "").strip()
        except Exception:
            return JSONResponse({"error": "Invalid request body"}, status_code=400)

        if not message:
            return JSONResponse({"error": "Commit message required"}, status_code=400)
        if len(message) > 500:
            return JSONResponse({"error": "Message too long (max 500 chars)"}, status_code=400)

        if not await app.state.git_op_lock.acquire("commit"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Stage all changes
            add_result = await run_subprocess(
                ["git", "add", "-A"],
                cwd=repo_path, capture_output=True, text=True, timeout=15
            )
            if add_result.returncode != 0:
                return JSONResponse({
                    "error": "git add failed",
                    "details": add_result.stderr
                }, status_code=500)

            # Check there's something to commit
            diff_result = await run_subprocess(
                ["git", "diff", "--cached", "--quiet"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if diff_result.returncode == 0:
                return JSONResponse({"error": "Nothing to commit"}, status_code=400)

            # Commit
            commit_result = await run_subprocess(
                ["git", "commit", "-m", message],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )
            if commit_result.returncode != 0:
                return JSONResponse({
                    "error": "Commit failed",
                    "details": commit_result.stderr
                }, status_code=500)

            # Parse short hash from output
            short_hash = ""
            for line in commit_result.stdout.split("\n"):
                if line.strip():
                    match = re.search(r'\[.+?\s+([a-f0-9]+)\]', line)
                    if match:
                        short_hash = match.group(1)
                    break

            app.state.audit_log.log("git_commit", {
                "message": message,
                "hash": short_hash,
            })

            return {
                "success": True,
                "hash": short_hash,
                "message": message,
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/git/push")
    async def git_push(
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Push current branch to its upstream remote."""

        # Validate target to prevent operating on wrong repo
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        if not await app.state.git_op_lock.acquire("push"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            branch_result = await run_subprocess(
                ["git", "branch", "--show-current"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
            if not branch:
                return JSONResponse({"error": "Not on a branch"}, status_code=400)

            push_result = await run_subprocess(
                ["git", "push", "-u", "origin", branch],
                cwd=repo_path, capture_output=True, text=True, timeout=60
            )
            if push_result.returncode != 0:
                return JSONResponse({
                    "error": "Push failed",
                    "details": push_result.stderr
                }, status_code=500)

            app.state.audit_log.log("git_push", {"branch": branch})

            return {
                "success": True,
                "branch": branch,
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Push timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()
