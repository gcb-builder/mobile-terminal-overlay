"""Routes for log viewing, session management, and transcript."""
import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import (
    get_project_id, run_subprocess, strip_ansi,
    get_cached_capture, set_cached_capture,
    get_cached_log, set_cached_log,
    get_cached_tool_output, set_cached_tool_output,
    TOOL_OUTPUT_MAX_CHARS,
    LOG_CACHE_DIR,
)

logger = logging.getLogger(__name__)


def _summarize_tool_result(tool_name: str, content, is_error: bool) -> str:
    """Summarize a tool_result into a short badge string."""
    # Normalize content to string
    if isinstance(content, list):
        # tool_result content can be a list of {type: "text", text: "..."}
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get('text', ''))
            elif isinstance(block, str):
                parts.append(block)
        text = '\n'.join(parts)
    elif isinstance(content, str):
        text = content
    else:
        text = str(content) if content else ''

    if is_error:
        # First non-empty line, stripped of ANSI escapes
        first_line = ''
        for ln in text.strip().split('\n'):
            stripped = strip_ansi(ln).strip()
            if stripped:
                first_line = stripped[:60]
                break
        return f"ERR: {first_line}" if first_line else 'ERR'

    line_count = len(text.split('\n')) if text.strip() else 0

    if tool_name == 'Bash':
        if line_count == 0:
            return 'OK'
        return f"OK {line_count}L"
    elif tool_name == 'Read':
        if line_count == 0:
            return '0L'
        return f"{line_count}L"
    elif tool_name == 'Grep':
        # Count "files with matches" style: lines that look like file paths
        file_count = 0
        for ln in text.strip().split('\n'):
            if ln.strip() and not ln.startswith(' '):
                file_count += 1
        if line_count == 0:
            return '0 matches'
        return f"{file_count}F {line_count}L"
    elif tool_name in ('Edit', 'Write'):
        return 'OK'
    elif tool_name == 'Glob':
        file_count = len([ln for ln in text.strip().split('\n') if ln.strip()]) if text.strip() else 0
        if file_count == 1:
            return '1 file'
        return f"{file_count} files"
    else:
        # Generic: show line count or short text inline
        if line_count == 0:
            return 'OK'
        return f"{line_count}L"


def register(app: FastAPI, deps):
    """Register log and transcript routes."""

    # --- Helper functions (closures over app for state access) ---

    def get_log_cache_path(project_id: str) -> Path:
        """Get the cache file path for a project's log."""
        LOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_CACHE_DIR / f"{project_id}.log"

    def read_cached_log(project_id: str) -> Optional[str]:
        """Read cached log content if it exists."""
        cache_path = get_log_cache_path(project_id)
        if cache_path.exists():
            try:
                return cache_path.read_text(errors="replace")
            except Exception as e:
                logger.warning(f"Error reading log cache: {e}")
        return None

    def write_log_cache(project_id: str, content: str):
        """Write log content to cache."""
        cache_path = get_log_cache_path(project_id)
        try:
            cache_path.write_text(content)
        except Exception as e:
            logger.warning(f"Error writing log cache: {e}")

    async def monitor_log_file_for_target(target_id: str):
        """
        Background task that monitors log file modifications to detect which
        .jsonl file belongs to the selected target.

        Watches for 10 seconds and associates any modified file with the target.
        """
        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return

        project_id = get_project_id(repo_path)
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        if not claude_projects_dir.exists():
            return

        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return

        # Skip if already cached
        if target_id in app.state.target_log_mapping:
            return

        # Record initial mtimes
        initial_mtimes = {str(f): f.stat().st_mtime for f in jsonl_files}
        logger.debug(f"Starting log file monitor for target {target_id}")

        # Monitor for 10 seconds
        for _ in range(20):
            await asyncio.sleep(0.5)

            # Check if target changed while we were monitoring
            if app.state.active_target != target_id:
                logger.debug(f"Target changed, stopping monitor for {target_id}")
                return

            # Check if already cached (by another code path)
            if target_id in app.state.target_log_mapping:
                return

            # Check for mtime changes
            for f in jsonl_files:
                try:
                    current_mtime = f.stat().st_mtime
                    if current_mtime > initial_mtimes.get(str(f), 0):
                        # File was modified - associate with this target (not pinned)
                        app.state.target_log_mapping[target_id] = {"path": str(f), "pinned": False}
                        app.state.permission_detector.set_log_file(f)
                        logger.info(f"Monitor detected log file for target {target_id}: {f.name}")
                        return
                except Exception:
                    pass

        logger.debug(f"Monitor timeout for target {target_id}, no file changes detected")

    def detect_target_log_file(target_id: Optional[str], session_name: str, claude_projects_dir: Path) -> Optional[Path]:
        """
        Detect which .jsonl log file belongs to a specific target pane.

        Strategy:
        1. Check cached mapping (pinned or monitor-detected)
        2. Fall back to most recently modified (only among non-team logs)
        """
        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return None

        # Check if we have a mapping for this target (pinned or detected)
        if target_id:
            cached = app.state.target_log_mapping.get(target_id)
            if cached:
                cached_path = Path(cached["path"]) if isinstance(cached, dict) else Path(cached)
                if cached_path.exists():
                    logger.debug(f"Using mapped log file for target {target_id}: {cached_path.name}")
                    return cached_path

        # Build set of log files claimed by OTHER targets (team members)
        claimed_paths = set()
        if target_id:
            for tid, mapping in app.state.target_log_mapping.items():
                if tid != target_id:
                    p = Path(mapping["path"]) if isinstance(mapping, dict) else Path(mapping)
                    claimed_paths.add(str(p))

        # Prefer unclaimed files (avoids picking a team member's log)
        unclaimed = [f for f in jsonl_files if str(f) not in claimed_paths]
        candidates = unclaimed if unclaimed else jsonl_files

        newest_file = max(candidates, key=lambda f: f.stat().st_mtime)
        logger.info(f"Using newest log file: {newest_file.name} (mtime-based, {len(claimed_paths)} claimed by others)")
        return newest_file

    # Expose monitor function for use by target/select route in server.py
    app.state._monitor_log_file_for_target = monitor_log_file_for_target

    # --- Routes ---

    @app.get("/api/transcript")
    async def get_transcript(
        _auth=Depends(deps.verify_token),
        lines: int = Query(500),
        session: Optional[str] = Query(None),
        pane: int = Query(0),
    ):
        """
        Get terminal transcript using tmux capture-pane.
        Returns last N lines of terminal history.

        Params:
            session: tmux session name (defaults to current_session)
            pane: pane index within session (default 0)
            lines: number of lines to capture (default 500)

        Uses same 300ms cache as /api/terminal/capture.
        """

        target_session = session or app.state.current_session
        if not target_session:
            return JSONResponse({"error": "No session"}, status_code=400)

        pane_id = str(pane)
        target = f"{target_session}:{0}.{pane}"

        # Check cache first
        cached = get_cached_capture(target_session, pane_id, lines)
        if cached and "text" in cached:
            return cached

        try:
            result = await run_subprocess(
                ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                if "can't find" in result.stderr.lower() or "no such" in result.stderr.lower():
                    return JSONResponse(
                        {"error": f"Target not found: {target}", "session": target_session, "pane": pane},
                        status_code=409,
                    )
                return JSONResponse(
                    {"error": f"tmux capture-pane failed: {result.stderr}"},
                    status_code=500,
                )
            response = {
                "text": result.stdout,
                "session": target_session,
                "pane": pane,
            }
            set_cached_capture(target_session, pane_id, lines, response)
            return response
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Capture timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Transcript error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # NOTE: Legacy process-based detection code removed in favor of simple mtime approach.
    # The complex detection (matching Claude process to log file via timestamps) was unreliable,
    # especially after plan mode transitions. Simple mtime-based selection is more robust.
    @app.get("/api/log")
    async def get_log(
        request: Request,
        _auth=Depends(deps.verify_token),
        limit: int = Query(200),
        session_id: Optional[str] = Query(None, description="Specific session UUID to view (for Docs browser)"),
        pane_id: Optional[str] = Query(None, description="Pane ID (window:pane) to get log for, avoids race with global state"),
    ):
        """
        Get the Claude conversation log from ~/.claude/projects/.
        Finds the most recently modified .jsonl file for the current repo.
        Parses JSONL and returns readable conversation text.
        Falls back to cached log if source is cleared (e.g., after /clear).

        If session_id is provided, loads that specific session log (read-only view).
        If pane_id is provided, uses that pane's cwd for repo path (avoids multi-tab race condition).
        """
        # Log client ID for debugging duplicate requests
        client_id = request.headers.get('X-Client-ID', 'unknown')[:8]
        logger.debug(f"[{client_id}] GET /api/log pane_id={pane_id}")


        # Use pane_id if provided (avoids race condition with multi-tab)
        # Otherwise fall back to global active_target
        target_id = pane_id or app.state.active_target

        # Get repo path - either from explicit pane cwd or global state
        if pane_id:
            # Get cwd directly from tmux for this pane
            try:
                parts = pane_id.split(":")
                if len(parts) == 2:
                    result = await run_subprocess(
                        ["tmux", "display-message", "-t", f"{app.state.current_session}:{parts[0]}.{parts[1]}", "-p", "#{pane_current_path}"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        repo_path = Path(result.stdout.strip())
                    else:
                        repo_path = deps.get_current_repo_path()
                else:
                    repo_path = deps.get_current_repo_path()
            except Exception:
                repo_path = deps.get_current_repo_path()
        else:
            repo_path = deps.get_current_repo_path()

        if not repo_path:
            return {"exists": False, "content": "", "error": "No repo path found"}

        # Convert repo path to Claude's project identifier format
        project_id = get_project_id(repo_path)
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        # Helper to return cached content
        def return_cached():
            cached = read_cached_log(project_id)
            if cached:
                return {
                    "exists": True,
                    "content": cached,
                    "path": str(claude_projects_dir),
                    "session": app.state.current_session,
                    "cached": True,
                }
            return {
                "exists": False,
                "content": "",
                "path": str(claude_projects_dir),
                "session": app.state.current_session,
            }

        if not claude_projects_dir.exists():
            return return_cached()

        # If specific session_id provided, load that directly (for Docs browser)
        if session_id:
            log_file = claude_projects_dir / f"{session_id}.jsonl"
            if not log_file.exists():
                return {"exists": False, "content": "", "error": f"Session not found: {session_id}"}
        else:
            # Detect which log file belongs to this target
            log_file = detect_target_log_file(target_id, app.state.current_session, claude_projects_dir)
            if not log_file:
                return return_cached()
            # Update permission detector with discovered log file
            if not app.state.permission_detector.log_file:
                app.state.permission_detector.set_log_file(log_file)

        # Check mtime-based cache - avoid re-parsing if file unchanged
        try:
            file_stat = log_file.stat()
            file_mtime = file_stat.st_mtime
            cached_result = get_cached_log(project_id, target_id, file_mtime)
            if cached_result:
                logger.debug(f"Log cache hit for {log_file.name}")
                return cached_result
        except Exception as e:
            logger.debug(f"Log cache check failed: {e}")
            file_mtime = 0

        try:
            raw_content = log_file.read_text(errors="replace")
            lines_list = raw_content.strip().split('\n')

            # Parse JSONL and extract conversation
            conversation = []
            # Forward-index: tool_use.id -> (conversation_index, tool_name)
            pending_tool_uses = {}

            for line in lines_list:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    msg_type = entry.get('type')
                    message = entry.get('message', {})

                    if msg_type == 'user':
                        content = message.get('content', '')
                        # Handle tool_result entries (content is a list)
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get('type') == 'tool_result':
                                    tool_use_id = block.get('tool_use_id', '')
                                    if tool_use_id and tool_use_id in pending_tool_uses:
                                        conv_idx, tool_name, tool_detail = pending_tool_uses.pop(tool_use_id)
                                        result_content = block.get('content', '')
                                        is_error = block.get('is_error', False)
                                        summary = _summarize_tool_result(tool_name, result_content, is_error)
                                        # Upgrade plain string to structured entry
                                        orig = conversation[conv_idx]
                                        text = orig["text"] if isinstance(orig, dict) else orig
                                        conversation[conv_idx] = {
                                            "text": text,
                                            "tool": {
                                                "name": tool_name,
                                                "detail": tool_detail,
                                                "tool_use_id": tool_use_id,
                                                "result_summary": summary,
                                                "result_status": "error" if is_error else "ok",
                                            },
                                        }
                        elif isinstance(content, str) and content.strip():
                            # Strip system-injected tags from user messages
                            cleaned = re.sub(r'<(?:system-reminder|task-notification)[^>]*>[\s\S]*?</(?:system-reminder|task-notification)>', '', content).strip()
                            if cleaned:
                                conversation.append(f"$ {cleaned}")

                    elif msg_type == 'assistant':
                        content = message.get('content', [])
                        if isinstance(content, str):
                            conversation.append(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get('type') == 'text':
                                        text = block.get('text', '')
                                        if text.strip():
                                            conversation.append(text)
                                    elif block.get('type') == 'tool_use':
                                        tool_name = block.get('name', 'tool')
                                        tool_id = block.get('id', '')
                                        tool_input = block.get('input', {})
                                        # Format tool call nicely
                                        tool_detail = ''
                                        if tool_name == 'Bash':
                                            tool_detail = tool_input.get('command', '')[:200]
                                            conversation.append(f"• Bash: {tool_detail}")
                                        elif tool_name in ('Read', 'Edit', 'Write', 'Glob', 'Grep'):
                                            tool_detail = (tool_input.get('file_path') or tool_input.get('path') or tool_input.get('pattern', ''))[:100]
                                            conversation.append(f"• {tool_name}: {tool_detail}")
                                        elif tool_name == 'AskUserQuestion':
                                            # Show questions with options for user to respond
                                            questions = tool_input.get('questions', [])
                                            for q in questions:
                                                qtext = q.get('question', '')
                                                opts = q.get('options', [])
                                                conversation.append(f"❓ {qtext}")
                                                for i, opt in enumerate(opts, 1):
                                                    label = opt.get('label', '')
                                                    desc = opt.get('description', '')
                                                    conversation.append(f"  {i}. {label}" + (f" - {desc}" if desc else ""))
                                        elif tool_name == 'EnterPlanMode':
                                            conversation.append("📋 Entering plan mode...")
                                        elif tool_name == 'ExitPlanMode':
                                            conversation.append("✅ Exiting plan mode")
                                        elif tool_name == 'Task':
                                            # Show agent spawning with description
                                            desc = tool_input.get('description', '')
                                            agent_type = tool_input.get('subagent_type', '')
                                            if desc:
                                                conversation.append(f"🤖 Task ({agent_type}): {desc[:80]}")
                                            else:
                                                conversation.append(f"🤖 Task: {agent_type}")
                                        elif tool_name == 'TodoWrite':
                                            # Show todo updates
                                            todos = tool_input.get('todos', [])
                                            in_progress = [t for t in todos if t.get('status') == 'in_progress']
                                            if in_progress:
                                                conversation.append(f"📝 {in_progress[0].get('activeForm', 'Working...')}")
                                        else:
                                            conversation.append(f"• {tool_name}")
                                        # Track tool_use for result pairing (only tool pills, not special entries)
                                        if tool_name not in ('AskUserQuestion', 'EnterPlanMode', 'ExitPlanMode', 'Task', 'TodoWrite') and tool_id:
                                            pending_tool_uses[tool_id] = (len(conversation) - 1, tool_name, tool_detail)
                except json.JSONDecodeError:
                    continue

            # Limit to last N messages
            if len(conversation) > limit:
                conversation = conversation[-limit:]
                truncated = True
            else:
                truncated = False

            # If log was cleared (empty), fall back to cache
            if not conversation:
                return return_cached()

            # Redact potential secrets from each message
            def redact_secrets(text):
                text = re.sub(r'(sk-[a-zA-Z0-9]{20,})', '[REDACTED_API_KEY]', text)
                text = re.sub(r'(ghp_[a-zA-Z0-9]{36,})', '[REDACTED_GITHUB_TOKEN]', text)
                return text

            def _msg_text(m):
                return m["text"] if isinstance(m, dict) else m

            # Build structured messages: str for plain, {text, tool} for tool entries
            messages = []
            for m in conversation:
                text = redact_secrets(_msg_text(m))
                if isinstance(m, dict) and "tool" in m:
                    messages.append({"text": text, "tool": m["tool"]})
                else:
                    messages.append(text)

            content = '\n\n'.join(
                m["text"] if isinstance(m, dict) else m for m in messages
            )

            # Cache the content for persistence across /clear
            write_log_cache(project_id, content)

            result = {
                "exists": True,
                "content": content,  # For backward compatibility
                "messages": messages,  # New: array of messages (preserves code blocks)
                "path": str(log_file),
                "session": app.state.current_session,
                "modified": file_mtime,  # Use cached mtime to avoid extra stat call
                "truncated": truncated,
            }

            # Cache parsed result for fast subsequent requests
            if file_mtime > 0:
                set_cached_log(project_id, target_id, file_mtime, result)
                logger.debug(f"Log cache set for {log_file.name}")

            return result
        except Exception as e:
            logger.error(f"Error reading log file: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.delete("/api/log/cache")
    async def clear_log_cache(_auth=Depends(deps.verify_token)):
        """Clear the cached log for the current project."""

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return {"cleared": False, "error": "No repo path found"}

        project_id = get_project_id(repo_path)
        cache_path = get_log_cache_path(project_id)

        if cache_path.exists():
            try:
                cache_path.unlink()
                logger.info(f"Cleared log cache: {cache_path}")
                return {"cleared": True, "path": str(cache_path)}
            except Exception as e:
                logger.error(f"Error clearing log cache: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        return {"cleared": False, "error": "No cache file exists"}

    @app.get("/api/log/sessions")
    async def list_log_sessions(_auth=Depends(deps.verify_token)):
        """
        List available log files for the current project directory.
        Returns metadata for each session log to allow manual selection.
        """
        from datetime import datetime


        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return {"sessions": [], "error": "No repo path found"}

        project_id = get_project_id(repo_path)
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        if not claude_projects_dir.exists():
            return {"sessions": [], "current": None, "detection_method": None}

        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return {"sessions": [], "current": None, "detection_method": None}

        # Get the currently detected/pinned log file
        target_id = app.state.active_target
        current_log = detect_target_log_file(target_id, app.state.current_session, claude_projects_dir)
        current_id = current_log.stem if current_log else None

        # Check if current is pinned
        cached = app.state.target_log_mapping.get(target_id) if target_id else None
        is_pinned = cached.get("pinned", False) if isinstance(cached, dict) else False
        detection_method = "pinned" if is_pinned else "auto"

        sessions = []
        for log_file in jsonl_files:
            try:
                stat = log_file.stat()
                session_id = log_file.stem

                # Get first user message as preview and session start time
                preview = ""
                started = None
                try:
                    with open(log_file, 'r') as f:
                        for line in f:
                            if not line.strip():
                                continue
                            entry = json.loads(line)
                            # Get timestamp from first entry
                            if started is None:
                                ts_str = entry.get('timestamp', '')
                                if not ts_str and 'snapshot' in entry:
                                    ts_str = entry['snapshot'].get('timestamp', '')
                                if ts_str:
                                    started = ts_str
                            # Get first user message as preview
                            if entry.get('type') == 'user':
                                msg = entry.get('message', {})
                                content = msg.get('content', '')
                                if isinstance(content, str) and content.strip():
                                    preview = content.strip()[:100]
                                    break
                except Exception:
                    pass

                sessions.append({
                    "id": session_id,
                    "started": started,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                    "size": stat.st_size,
                    "preview": preview,
                    "is_current": session_id == current_id,
                    "is_pinned": is_pinned and session_id == current_id,
                })
            except Exception as e:
                logger.debug(f"Error reading log file {log_file}: {e}")
                continue

        # Sort by modification time (most recent first)
        sessions.sort(key=lambda s: s.get("modified", ""), reverse=True)

        return {
            "sessions": sessions,
            "current": current_id,
            "detection_method": detection_method,
        }

    def _resolve_log_file(pane_id: Optional[str] = None, session_id: Optional[str] = None) -> Optional[Path]:
        """Resolve the active JSONL log file path.

        Used by /api/log and /api/log/tool-output to avoid duplicating
        the repo-path -> project-id -> log-file resolution logic.
        Returns None if no log file can be found.
        """
        target_id = pane_id or app.state.active_target
        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return None
        project_id = get_project_id(repo_path)
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id
        if not claude_projects_dir.exists():
            return None
        if session_id:
            log_file = claude_projects_dir / f"{session_id}.jsonl"
            return log_file if log_file.exists() else None
        return detect_target_log_file(target_id, app.state.current_session, claude_projects_dir)

    @app.get("/api/log/tool-output")
    async def get_tool_output(
        _auth=Depends(deps.verify_token),
        tool_use_id: str = Query(..., description="tool_use_id to fetch result for"),
        pane_id: Optional[str] = Query(None),
        session_id: Optional[str] = Query(None),
    ):
        """
        Return the full tool_result content for a specific tool_use_id.
        Reverse-scans JSONL for fast lookup (recent results found first).
        """
        if not tool_use_id:
            return JSONResponse({"error": "tool_use_id required"}, status_code=400)

        log_file = _resolve_log_file(pane_id=pane_id, session_id=session_id)
        if not log_file:
            return JSONResponse({"error": "No log file found"}, status_code=404)

        try:
            file_mtime = log_file.stat().st_mtime
        except Exception:
            return JSONResponse({"error": "Cannot stat log file"}, status_code=500)

        # Check cache
        cached = get_cached_tool_output(str(log_file), file_mtime, tool_use_id)
        if cached:
            return cached

        # Reverse-scan JSONL lines for the matching tool_result
        try:
            raw = log_file.read_text(errors="replace")
            lines_list = raw.strip().split('\n')

            for line in reversed(lines_list):
                if not line.strip() or tool_use_id not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get('type') != 'user':
                    continue
                content = entry.get('message', {}).get('content', '')
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') != 'tool_result':
                        continue
                    if block.get('tool_use_id') != tool_use_id:
                        continue
                    # Found it
                    result_content = block.get('content', '')
                    # Normalize list content to string
                    if isinstance(result_content, list):
                        parts = []
                        for b in result_content:
                            if isinstance(b, dict):
                                parts.append(b.get('text', ''))
                            elif isinstance(b, str):
                                parts.append(b)
                        result_content = '\n'.join(parts)
                    elif not isinstance(result_content, str):
                        result_content = str(result_content) if result_content else ''

                    is_error = block.get('is_error', False)
                    truncated = len(result_content) > TOOL_OUTPUT_MAX_CHARS
                    if truncated:
                        result_content = result_content[:TOOL_OUTPUT_MAX_CHARS]

                    result = {
                        "tool_use_id": tool_use_id,
                        "content": result_content,
                        "is_error": is_error,
                        "line_count": len(result_content.split('\n')) if result_content.strip() else 0,
                        "char_count": len(result_content),
                        "truncated": truncated,
                    }
                    set_cached_tool_output(str(log_file), file_mtime, tool_use_id, result)
                    return result

            return JSONResponse({"error": f"tool_use_id not found: {tool_use_id}"}, status_code=404)

        except Exception as e:
            logger.error(f"Error reading tool output: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/log/select")
    async def select_log_session(
        _auth=Depends(deps.verify_token),
        session_id: str = Query(..., description="Session UUID to pin"),
    ):
        """
        Pin a specific log file to the current target.
        This overrides auto-detection until unpinned.
        """

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path found"}, status_code=400)

        project_id = get_project_id(repo_path)
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        log_file = claude_projects_dir / f"{session_id}.jsonl"
        if not log_file.exists():
            return JSONResponse({"error": f"Log file not found: {session_id}"}, status_code=404)

        target_id = app.state.active_target
        if not target_id:
            return JSONResponse({"error": "No active target selected"}, status_code=400)

        # Pin the log file to this target
        app.state.target_log_mapping[target_id] = {"path": str(log_file), "pinned": True}
        app.state.permission_detector.set_log_file(log_file)
        logger.info(f"Pinned log file for target {target_id}: {session_id}")

        return {
            "success": True,
            "target": target_id,
            "session_id": session_id,
            "pinned": True,
        }

    @app.post("/api/log/unpin")
    async def unpin_log_session(_auth=Depends(deps.verify_token)):
        """
        Unpin the current target's log file, reverting to auto-detection.
        """

        target_id = app.state.active_target
        if not target_id:
            return JSONResponse({"error": "No active target selected"}, status_code=400)

        cached = app.state.target_log_mapping.get(target_id)
        if not cached:
            return {"success": True, "message": "No mapping to unpin"}

        # Remove the mapping so auto-detection runs again
        del app.state.target_log_mapping[target_id]
        logger.info(f"Unpinned log file for target {target_id}")

        return {
            "success": True,
            "target": target_id,
            "message": "Reverted to auto-detection",
        }

    # ----- Activity Timeline -----

    _activity_cache = {}  # (project_id, pane_id) -> (ts, mtime, result)
    _ACTIVITY_CACHE_TTL = 2.0

    CATEGORY_ICONS = {
        "tools": "\u2699",
        "files": "\U0001f4dd",
        "tests": "\u2713",
        "git": "\U0001f500",
        "errors": "\u26a0",
    }

    def _classify_event_category(tool_name: str, tool_input: dict) -> str:
        """Classify a tool call into an activity category."""
        if tool_name in ("Edit", "Write"):
            return "files"
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd.strip().startswith("git ") or "| git " in cmd:
                return "git"
            if any(kw in cmd for kw in ("pytest", "npm test", "cargo test", "go test", "jest", "vitest", "make test")):
                return "tests"
        return "tools"

    def _build_event(tool_name, tool_id, tool_input, timestamp, ts_epoch):
        """Build one activity event from a tool_use block."""
        category = _classify_event_category(tool_name, tool_input)
        title = tool_name
        detail = ""

        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            detail = cmd[:300]
            if category == "git":
                parts = cmd.strip().split()
                title = f"git {parts[1]}" if len(parts) > 1 else "git"
            elif category == "tests":
                title = f"Test: {cmd[:60]}"
            else:
                title = f"Bash: {cmd[:80]}"
        elif tool_name in ("Edit", "Write"):
            path = tool_input.get("file_path", "")
            short = path.rsplit("/", 1)[-1] if "/" in path else path
            title = f"{tool_name}: {short}"
            detail = path
        elif tool_name == "Read":
            path = tool_input.get("file_path", "")
            short = path.rsplit("/", 1)[-1] if "/" in path else path
            title = f"Read: {short}"
            detail = path
        elif tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            title = f"{tool_name}: {pattern[:60]}"
            detail = pattern
        else:
            title = tool_name

        return {
            "id": tool_id or f"evt-{ts_epoch}",
            "timestamp": timestamp,
            "ts_epoch": ts_epoch,
            "category": category,
            "icon": CATEGORY_ICONS.get(category, "\u2022"),
            "title": title,
            "detail": detail,
            "status": None,
            "status_badge": None,
            "tool_use_id": tool_id or None,
            "_tool_name": tool_name,
        }

    def _parse_activity_events(raw_content: str, limit: int = 100, category: Optional[str] = None) -> list:
        """Parse JSONL content into activity timeline events."""
        events = []
        pending = {}  # tool_use_id -> event index

        for line in raw_content.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = entry.get("type")
            message = entry.get("message", {})
            timestamp = entry.get("timestamp", "")

            ts_epoch = 0
            if timestamp:
                try:
                    # ISO format: 2025-01-15T10:30:00.000Z
                    from datetime import datetime, timezone
                    ts = timestamp.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ts)
                    ts_epoch = int(dt.timestamp() * 1000)
                except Exception:
                    pass

            if msg_type == "assistant":
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue
                group_id = f"turn_{len(events)}"
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name", "")
                    tool_id = block.get("id", "")
                    tool_input = block.get("input", {})
                    # Skip phase tools — they go in banner, not feed
                    if tool_name in ("EnterPlanMode", "ExitPlanMode", "TodoWrite",
                                     "Task", "AskUserQuestion", "TaskCreate",
                                     "TaskUpdate", "TaskList", "TaskGet"):
                        continue
                    evt = _build_event(tool_name, tool_id, tool_input, timestamp, ts_epoch)
                    evt["group_id"] = group_id
                    idx = len(events)
                    events.append(evt)
                    if tool_id:
                        pending[tool_id] = idx

            elif msg_type == "user":
                content = message.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tid = block.get("tool_use_id", "")
                            if tid and tid in pending:
                                idx = pending.pop(tid)
                                result_content = block.get("content", "")
                                is_error = block.get("is_error", False)
                                tool_nm = events[idx].get("_tool_name", "")
                                summary = _summarize_tool_result(tool_nm, result_content, is_error)
                                events[idx]["status"] = "error" if is_error else "ok"
                                events[idx]["status_badge"] = summary
                                if is_error:
                                    events[idx]["category"] = "errors"
                                    events[idx]["icon"] = CATEGORY_ICONS["errors"]

        # Filter
        if category and category != "all":
            events = [e for e in events if e.get("category") == category]

        # Clean internal fields, reverse to newest-first
        for e in events:
            e.pop("_tool_name", None)
        events.reverse()
        return events[:limit]

    @app.get("/api/activity")
    async def get_activity(
        request: Request,
        _auth=Depends(deps.verify_token),
        limit: int = Query(100),
        category: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Activity timeline: structured event feed from JSONL logs."""
        log_file = _resolve_log_file(pane_id=pane_id)
        if not log_file or not log_file.exists():
            return {"events": [], "phase": None, "truncated": False, "modified": 0}

        try:
            file_mtime = log_file.stat().st_mtime
        except Exception:
            return {"events": [], "phase": None, "truncated": False, "modified": 0}

        # Cache check
        target_id = pane_id or app.state.active_target or ""
        project_id = get_project_id(deps.get_current_repo_path() or Path("."))
        cache_key = (project_id, target_id, category or "all")
        cached = _activity_cache.get(cache_key)
        import time as _time
        if cached:
            ts, cached_mtime, cached_result = cached
            if _time.time() - ts < _ACTIVITY_CACHE_TTL and cached_mtime == file_mtime:
                return cached_result

        raw = log_file.read_text(errors="replace")
        events = _parse_activity_events(raw, limit=limit, category=category)
        truncated = len(events) >= limit

        result = {
            "events": events,
            "phase": None,
            "truncated": truncated,
            "modified": file_mtime,
        }

        # Cache result
        _activity_cache[cache_key] = (_time.time(), file_mtime, result)
        # Evict stale
        now = _time.time()
        stale = [k for k, (ts, _, _) in _activity_cache.items() if now - ts > _ACTIVITY_CACHE_TTL * 10]
        for k in stale:
            del _activity_cache[k]

        return result
