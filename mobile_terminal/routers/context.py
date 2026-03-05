"""Routes for context, plans, and docs."""
import logging
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import get_plan_links, save_plan_links, get_plans_for_repo

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register context / plan / docs routes."""

    def _read_claude_file(filename: str, label: str):
        """Read a file from the current repo's .claude/ directory."""
        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return {"exists": False, "content": "", "session": app.state.current_session}

        target_file = repo_path / ".claude" / filename
        if not target_file.exists():
            return {
                "exists": False,
                "content": "",
                "path": str(target_file),
                "session": app.state.current_session,
            }

        try:
            content = target_file.read_text(errors="replace")
            return {
                "exists": True,
                "content": content,
                "path": str(target_file),
                "session": app.state.current_session,
                "modified": target_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading {label}: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/context")
    async def get_context(_auth=Depends(deps.verify_token)):
        """Get the .claude/CONTEXT.md file from the current repo."""
        return _read_claude_file("CONTEXT.md", "context file")

    @app.get("/api/touch")
    async def get_touch(_auth=Depends(deps.verify_token)):
        """Get the .claude/touch-summary.md file from the current repo."""
        return _read_claude_file("touch-summary.md", "touch file")

    @app.get("/api/plan")
    async def get_plan(
        _auth=Depends(deps.verify_token),
        filename: str = Query(..., description="Plan filename"),
        preview: bool = Query(True, description="Return only first 10 lines"),
    ):
        """Get a plan file from ~/.claude/plans/."""
        if not re.match(r'^[\w\-\.]+\.md$', filename):
            return JSONResponse({"error": "Invalid filename"}, status_code=400)

        plan_file = Path.home() / ".claude" / "plans" / filename

        if not plan_file.exists():
            return {"exists": False, "content": "", "filename": filename}

        try:
            content = plan_file.read_text(errors="replace")
            if preview:
                lines = content.split('\n')[:10]
                content = '\n'.join(lines)
                if len(content.split('\n')) > 10:
                    content += '\n...'

            return {
                "exists": True,
                "content": content,
                "filename": filename,
                "modified": plan_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading plan file: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/plan/active")
    async def get_active_plan(
        _auth=Depends(deps.verify_token),
        preview: bool = Query(True, description="Return only first 15 lines"),
    ):
        """Get the active plan for the current repo."""
        plans_dir = Path.home() / ".claude" / "plans"
        if not plans_dir.exists():
            return {"status": "none", "exists": False, "error": "No plans directory"}

        repo_path = deps.get_current_repo_path()
        repo_str = str(repo_path) if repo_path else None

        def read_plan_content(plan_path: Path, preview_mode: bool) -> str:
            content = plan_path.read_text(errors="replace")
            if preview_mode:
                lines = content.split('\n')[:15]
                content = '\n'.join(lines)
                if len(content.split('\n')) > 15:
                    content += '\n...'
            return content

        def plan_response(plan_path: Path, status: str, linked: bool = False):
            return {
                "status": status,
                "exists": True,
                "content": read_plan_content(plan_path, preview),
                "filename": plan_path.name,
                "modified": plan_path.stat().st_mtime,
                "linked": linked,
                "repo": repo_str,
            }

        # Priority 1: Check explicit link
        if repo_str:
            links = get_plan_links()
            for filename, link_data in links.items():
                if link_data.get("repo") == repo_str:
                    plan_path = plans_dir / filename
                    if plan_path.exists():
                        return plan_response(plan_path, "found", linked=True)

        # Priority 2: Content grep with scoring
        if repo_path:
            matches = get_plans_for_repo(repo_path)
            if len(matches) == 1:
                return plan_response(matches[0][0], "found")
            elif len(matches) > 1:
                top_score = matches[0][1]
                close_matches = [(p, s) for p, s in matches if s >= top_score - 1]
                if len(close_matches) > 1:
                    candidates = []
                    for plan_path, score in close_matches[:5]:
                        candidates.append({
                            "filename": plan_path.name,
                            "score": score,
                            "modified": plan_path.stat().st_mtime,
                            "preview": read_plan_content(plan_path, True)[:200],
                        })
                    return {
                        "status": "ambiguous",
                        "exists": True,
                        "candidates": candidates,
                        "repo": repo_str,
                    }
                else:
                    return plan_response(matches[0][0], "found")

        # Fallback: most recent plan (global)
        plan_files = list(plans_dir.glob("*.md"))
        if not plan_files:
            return {"status": "none", "exists": False, "error": "No plan files"}

        plan_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return {
            "status": "none",
            "exists": True,
            "fallback": True,
            "content": read_plan_content(plan_files[0], preview),
            "filename": plan_files[0].name,
            "modified": plan_files[0].stat().st_mtime,
            "repo": repo_str,
        }

    @app.get("/api/plans")
    async def list_all_plans(_auth=Depends(deps.verify_token)):
        """List all plan files for manual browsing/selection."""
        plans_dir = Path.home() / ".claude" / "plans"
        if not plans_dir.exists():
            return {"plans": []}

        plan_files = list(plans_dir.glob("*.md"))
        plan_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        plans = []
        for p in plan_files:
            try:
                content = p.read_text(errors="replace")
                title = content.split('\n')[0].lstrip('#').strip() or p.name
                preview = content[:200]
                plans.append({
                    "filename": p.name,
                    "title": title,
                    "preview": preview,
                    "modified": p.stat().st_mtime,
                })
            except Exception:
                plans.append({
                    "filename": p.name,
                    "title": p.name,
                    "preview": "",
                    "modified": p.stat().st_mtime,
                })

        return {"plans": plans}

    @app.post("/api/plan/link")
    async def link_plan(
        filename: str = Query(..., description="Plan filename to link"),
        _auth=Depends(deps.verify_token),
    ):
        """Link a plan to the current repo."""
        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path found"}, status_code=400)

        plans_dir = Path.home() / ".claude" / "plans"
        plan_path = plans_dir / filename
        if not plan_path.exists():
            return JSONResponse({"error": f"Plan file not found: {filename}"}, status_code=404)

        repo_str = str(repo_path)
        links = get_plan_links()

        # Remove any existing link for this repo (one plan per repo)
        links = {k: v for k, v in links.items() if v.get("repo") != repo_str}

        # Add new link
        from datetime import datetime
        links[filename] = {
            "repo": repo_str,
            "linked_at": datetime.utcnow().isoformat() + "Z",
        }
        save_plan_links(links)

        logger.info(f"Linked plan {filename} to repo {repo_str}")
        return {"success": True, "filename": filename, "repo": repo_str}

    @app.delete("/api/plan/link")
    async def unlink_plan(_auth=Depends(deps.verify_token)):
        """Unlink the plan for the current repo."""
        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path found"}, status_code=400)

        repo_str = str(repo_path)
        links = get_plan_links()

        removed = None
        for filename, link_data in list(links.items()):
            if link_data.get("repo") == repo_str:
                removed = filename
                del links[filename]
                break

        if removed:
            save_plan_links(links)
            logger.info(f"Unlinked plan {removed} from repo {repo_str}")
            return {"success": True, "removed": removed, "repo": repo_str}
        else:
            return {"success": False, "error": "No plan linked to this repo"}

    @app.get("/api/docs/context")
    async def get_context_doc(_auth=Depends(deps.verify_token)):
        """Read .claude/CONTEXT.md for display in Docs modal."""
        return _read_claude_file("CONTEXT.md", "CONTEXT.md")

    @app.get("/api/docs/touch")
    async def get_touch_summary(_auth=Depends(deps.verify_token)):
        """Read .claude/touch-summary.md for display in Docs modal."""
        return _read_claude_file("touch-summary.md", "touch-summary.md")
