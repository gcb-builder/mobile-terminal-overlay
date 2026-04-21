"""Routes for file browsing, search, and upload."""
import logging
import os
import subprocess
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Query, UploadFile
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import get_project_id, run_subprocess


def _agent_memory_dir(repo_path: Path) -> Path:
    """Return the agent's persistent-memory directory for a given repo
    (~/.claude/projects/<project_id>/memory/). Lives outside the repo
    so it's not committable; we surface it as a `memory/` virtual
    prefix in MTO's file searcher."""
    return Path.home() / ".claude" / "projects" / get_project_id(repo_path) / "memory"

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register file-related routes."""

    @app.get("/api/workspace/dirs")
    async def list_workspace_dirs(_auth=Depends(deps.verify_token)):
        """
        List directories under configured workspace_dirs for new window creation.
        Excludes hidden dirs and dirs already in config.repos.
        """
        config = app.state.config

        # Build set of resolved repo paths for exclusion
        repo_paths = set()
        for repo in config.repos:
            try:
                repo_paths.add(str(Path(repo.path).expanduser().resolve()))
            except Exception:
                pass

        dirs = []
        ws_dirs = config.workspace_dirs or [str(Path.home() / "dev")]
        for ws_dir in ws_dirs:
            ws_path = Path(ws_dir).expanduser()
            if not ws_path.is_dir():
                continue
            parent_display = ws_dir  # Keep original form (e.g. "~/dev")
            try:
                entries = sorted(os.scandir(ws_path), key=lambda e: e.name.lower())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                if entry.name.startswith('.'):
                    continue
                resolved = str(Path(entry.path).resolve())
                if resolved in repo_paths:
                    continue
                dirs.append({
                    "name": entry.name,
                    "path": resolved,
                    "parent": parent_display,
                })
                if len(dirs) >= 200:
                    break
            if len(dirs) >= 200:
                break

        return {"dirs": dirs}

    @app.get("/api/repos")
    async def list_repos(_auth=Depends(deps.verify_token)):
        """
        List configured repos available for new window creation.
        """
        config = app.state.config

        repos_list = []
        for repo in config.repos:
            repo_path = Path(repo.path)
            repos_list.append({
                "label": repo.label,
                "path": repo.path,
                "session": repo.session,
                "exists": repo_path.exists(),
                "startup_command": repo.startup_command,
                "startup_delay_ms": repo.startup_delay_ms,
            })

        return {
            "repos": repos_list,
            "current_session": app.state.current_session
        }

    @app.get("/api/files/tree")
    async def list_files_tree(_auth=Depends(deps.verify_token)):
        """
        List all files in repo as a flat list grouped by directory.
        Uses git ls-files to respect .gitignore.
        """

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return {"files": [], "directories": [], "root": None}

        try:
            # Get tracked + untracked files (excluding ignored)
            result = await run_subprocess(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )

            if result.returncode != 0:
                # Fallback: list files excluding .git
                result = await run_subprocess(
                    ["find", ".", "-type", "f", "-not", "-path", "./.git/*"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo_path,
                )
                files = sorted([f.lstrip("./") for f in result.stdout.strip().split("\n") if f])
            else:
                files = sorted(set(result.stdout.strip().split("\n")))

            # Build directory structure
            directories = set()
            for f in files:
                parts = f.split("/")
                for i in range(1, len(parts)):
                    directories.add("/".join(parts[:i]))

            return {
                "files": files,
                "directories": sorted(directories),
                "root": str(repo_path),
                "root_name": repo_path.name
            }

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Listing timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"File tree error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/files/search")
    async def search_files(q: str = Query(""), _auth=Depends(deps.verify_token), limit: int = Query(20)):
        """
        Search files in the current repo.
        Uses git ls-files to respect .gitignore.
        """

        if not q or len(q) < 1:
            return {"files": []}

        try:
            repo_path = deps.get_current_repo_path()
            cwd = str(repo_path) if repo_path else None

            # Get list of tracked files using git ls-files
            result = await run_subprocess(
                ["git", "ls-files"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=cwd,
            )

            if result.returncode != 0:
                # Fallback to find if not a git repo
                result = await run_subprocess(
                    ["find", ".", "-type", "f", "-name", f"*{q}*", "-not", "-path", "./.git/*"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=cwd,
                )
                files = [f.lstrip("./") for f in result.stdout.strip().split("\n") if f][:limit]
            else:
                # Filter files by query (case-insensitive)
                all_files = result.stdout.strip().split("\n")
                q_lower = q.lower()
                files = [f for f in all_files if q_lower in f.lower()][:limit]

            # Also include the agent's persistent-memory dir under a
            # virtual `memory/` prefix. These files live outside the
            # repo (~/.claude/projects/<id>/memory/) so git ls-files
            # never sees them, but they're useful project context to
            # browse from MTO.
            try:
                if repo_path:
                    mem_dir = _agent_memory_dir(repo_path)
                    if mem_dir.exists():
                        q_lower = q.lower()
                        for p in sorted(mem_dir.rglob("*")):
                            if not p.is_file():
                                continue
                            virtual = "memory/" + str(p.relative_to(mem_dir))
                            if q_lower in virtual.lower():
                                if virtual not in files:
                                    files.append(virtual)
                        files = files[:limit]
            except Exception as e:
                logger.debug(f"memory dir scan failed: {e}")

            return {"files": files, "query": q}

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Search timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"File search error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/file")
    async def read_file(path: str = Query(...), _auth=Depends(deps.verify_token)):
        """
        Read a file from the current repo, or — if path starts with
        `memory/` — from the agent's persistent-memory directory.
        Path is relative to that root.
        """

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path"}, status_code=400)

        # Sanitize path - no parent traversal
        if ".." in path or path.startswith("/"):
            return JSONResponse({"error": "Invalid path"}, status_code=400)

        # `memory/...` is the virtual prefix used by /api/files/search
        # for the agent's persistent-memory directory. Resolve against
        # that root instead of the repo.
        if path.startswith("memory/"):
            mem_root = _agent_memory_dir(repo_path).resolve()
            if not mem_root.exists():
                return JSONResponse({"error": "Memory dir not found"}, status_code=404)
            file_path = (mem_root / path[len("memory/"):]).resolve()
            if not str(file_path).startswith(str(mem_root)):
                return JSONResponse({"error": "Invalid path"}, status_code=400)
        else:
            file_path = (repo_path / path).resolve()
            # Ensure resolved path is still within repo (catches symlink escapes)
            if not str(file_path).startswith(str(repo_path.resolve())):
                return JSONResponse({"error": "Invalid path"}, status_code=400)

        if not file_path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)

        if not file_path.is_file():
            return JSONResponse({"error": "Not a file"}, status_code=400)

        # Limit file size (1MB)
        if file_path.stat().st_size > 1024 * 1024:
            return JSONResponse({"error": "File too large"}, status_code=413)

        try:
            content = file_path.read_text(errors="replace")
            return {"path": path, "content": content}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/upload")
    async def upload_file(
        file: UploadFile = File(...),
        _auth=Depends(deps.verify_token),
    ):
        """
        Upload an image or document file for use in terminal prompts.

        Saves to .claude/uploads/ directory (git-ignored).
        Returns the relative path for insertion into terminal.
        """

        # Validate content type
        IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
        DOC_TYPES = {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/json",
            "application/xml",
            "application/zip",
            "application/gzip",
            "application/x-tar",
            "application/x-yaml",
            "application/toml",
        }
        BLOCKED_TYPES = {"application/x-executable", "application/x-sharedlib"}
        ct = file.content_type or "application/octet-stream"
        allowed = (
            ct in IMAGE_TYPES
            or ct in DOC_TYPES
            or ct.startswith("text/")  # text/plain, text/html, text/css, text/javascript, text/yaml, etc.
            or ct == "application/octet-stream"  # generic fallback for unknown extensions
        )
        if not allowed or ct in BLOCKED_TYPES:
            return JSONResponse(
                {"error": f"Invalid file type: {ct}"},
                status_code=400,
            )

        # Read file content and check size (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        content = await file.read()
        if len(content) > max_size:
            return JSONResponse(
                {"error": f"File too large: {len(content)} bytes. Max: {max_size} bytes"},
                status_code=400,
            )

        # Save uploads into the configured repo for the current session.
        # This ensures the file lands in the repo root's .claude/uploads/
        # where Claude Code can reliably find it via absolute path.
        config = app.state.config
        session = app.state.current_session
        repo_path = None
        for repo in config.repos:
            if repo.session == session:
                repo_path = Path(repo.path).expanduser().resolve()
                break
        if not repo_path:
            repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse(
                {"error": "No repo path available for uploads"},
                status_code=400,
            )

        uploads_dir = repo_path / ".claude" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "bin"
        timestamp = int(time.time() * 1000)
        filename = f"upload-{timestamp}.{ext}"
        filepath = uploads_dir / filename

        # Always return absolute path so it resolves regardless of Claude's CWD
        abs_path = filepath.resolve()

        # Write file
        try:
            with open(filepath, "wb") as f:
                f.write(content)
            logger.info(f"Uploaded file: {filepath}")
            return {"path": str(abs_path), "filename": filename, "size": len(content)}
        except Exception as e:
            logger.error(f"Failed to save upload: {e}")
            return JSONResponse({"error": "Failed to save file"}, status_code=500)
