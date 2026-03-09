"""Routes for file browsing, search, and upload."""
import logging
import os
import subprocess
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Query, UploadFile
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import run_subprocess

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
        for ws_dir in config.workspace_dirs:
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
            # Get list of tracked files using git ls-files
            result = await run_subprocess(
                ["git", "ls-files"],
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
                files = sorted(result.stdout.strip().split("\n"))

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

            return {"files": files, "query": q}

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Search timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"File search error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/file")
    async def read_file(path: str = Query(...), _auth=Depends(deps.verify_token)):
        """
        Read a file from the current repo.
        Path is relative to repo root.
        """

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path"}, status_code=400)

        # Sanitize path - no parent traversal
        if ".." in path or path.startswith("/"):
            return JSONResponse({"error": "Invalid path"}, status_code=400)

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
            "text/plain", "text/csv", "text/markdown",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        allowed_types = IMAGE_TYPES | DOC_TYPES
        if file.content_type not in allowed_types:
            return JSONResponse(
                {"error": f"Invalid file type: {file.content_type}. Allowed: images (png, jpeg, webp, gif), documents (pdf, doc, docx, xls, xlsx, txt, csv, md)"},
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

        # Create uploads directory inside the active repo so the path
        # resolves relative to Claude's CWD in that repo.
        repo_path = deps.get_current_repo_path()
        if repo_path:
            uploads_dir = repo_path / ".claude" / "uploads"
        else:
            uploads_dir = Path(".claude/uploads")
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "bin"
        timestamp = int(time.time() * 1000)
        filename = f"upload-{timestamp}.{ext}"
        filepath = uploads_dir / filename

        # Return path relative to repo root so it resolves from Claude's CWD
        if repo_path:
            rel_path = filepath.relative_to(repo_path)
        else:
            rel_path = filepath

        # Write file
        try:
            with open(filepath, "wb") as f:
                f.write(content)
            logger.info(f"Uploaded file: {filepath}")
            return {"path": str(rel_path), "filename": filename, "size": len(content)}
        except Exception as e:
            logger.error(f"Failed to save upload: {e}")
            return JSONResponse({"error": "Failed to save file"}, status_code=500)
