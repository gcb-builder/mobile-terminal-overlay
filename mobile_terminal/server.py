"""
FastAPI server for Mobile Terminal Overlay.

Provides:
- Static file serving for the web UI
- WebSocket endpoint for terminal I/O
- Token-based authentication
"""

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from mobile_terminal.drivers import get_driver, ClaudePermissionDetector, ObserveContext
from mobile_terminal.helpers import (
    get_project_id,
    run_subprocess, get_tmux_target, get_bounded_snapshot,
    get_plan_links, save_plan_links, score_plan_for_repo, get_plans_for_repo,
    list_tmux_sessions, _tmux_session_exists, _list_session_windows,
    _match_repo_to_window, _get_pane_command, _create_tmux_window,
    _send_startup_command, ensure_tmux_setup,
    _sigchld_handler, _resolve_device,
    STATIC_DIR, PLAN_LINKS_FILE,
)


from mobile_terminal.models import (
    RingBuffer, SnapshotBuffer, AuditLog, GitOpLock, InputQueue,
    QueueItem, CommandQueue, BacklogStore,
    QUEUE_DIR, get_queue_file, load_queue_from_disk, save_queue_to_disk,
)
from mobile_terminal.runtime import TmuxRuntime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Config, DeviceConfig, Repo

logger = logging.getLogger(__name__)


def create_app(config: Config) -> FastAPI:
    """
    Create FastAPI application with configured routes.

    Args:
        config: Configuration instance.

    Returns:
        Configured FastAPI app.
    """
    app = FastAPI(
        title="Mobile Terminal Overlay",
        description="Mobile-optimized terminal UI for tmux sessions",
        version="0.1.0",
    )

    # Store config and state on app
    app.state.config = config
    app.state.no_auth = config.no_auth
    app.state.token = None if config.no_auth else (config.token or secrets.token_urlsafe(16))

    # ProcessRuntime — single owner of PTY lifecycle and tmux commands
    runtime = TmuxRuntime()
    app.state.runtime = runtime

    app.state.active_client = None
    app.state.read_task = None
    app.state.current_session = config.session_name  # Track current session
    app.state.last_ws_connect = 0  # Timestamp of last WebSocket connection
    app.state.ws_connect_lock = asyncio.Lock()  # Prevent concurrent connection handling
    app.state.output_buffer = RingBuffer(max_size=2 * 1024 * 1024)  # 2MB scrollback buffer
    app.state.input_queue = InputQueue(runtime=runtime)  # Serialized input queue with ACKs
    app.state.command_queue = CommandQueue()  # Deferred-send command queue
    app.state.command_queue.set_app(app)
    app.state.backlog_store = BacklogStore()  # Project-scoped deferred work items
    app.state.backlog_store.set_app(app)

    from mobile_terminal.permission_policy import PermissionPolicy
    app.state.permission_policy = PermissionPolicy()
    app.state.permission_policy.load()
    app.state.snapshot_buffer = SnapshotBuffer()  # Preview snapshots ring buffer
    app.state.audit_log = AuditLog()  # Audit log for rollback operations
    app.state.git_op_lock = GitOpLock()  # Lock for git write operations
    # Restore last active target from disk, or default to None
    _saved_target = None
    try:
        _target_file = Path.home() / ".cache" / "mobile-overlay" / "active_target.txt"
        if _target_file.exists():
            _saved_target = _target_file.read_text().strip() or None
    except Exception:
        pass
    app.state.active_target = _saved_target
    app.state.target_log_mapping = {}  # Maps pane_id -> {"path": str, "pinned": bool}
    app.state.last_restart_time = 0.0  # Timestamp of last server restart request
    app.state.target_epoch = 0  # Incremented on each target switch for cache invalidation
    # spawn_tmux is now in runtime.spawn()
    app.state.setup_result = None  # Result from ensure_tmux_setup()
    app.state.last_ws_input_time = 0  # Last time mobile client sent input (for desktop activity detection)
    app.state.permission_detector = ClaudePermissionDetector()  # JSONL-based permission prompt detector
    from mobile_terminal.drivers.claude import BacklogCandidateDetector
    from mobile_terminal.models import CandidateStore
    app.state.candidate_detector = BacklogCandidateDetector()
    app.state.candidate_store = CandidateStore()
    app.state.driver = get_driver(config.agent_type, config.agent_display_name)

    from mobile_terminal.scratch import ScratchStore
    app.state.scratch_store = ScratchStore(
        max_bytes=getattr(config, "scratch_max_mb", 50) * 1024 * 1024
    )

    async def send_typed(ws, msg_type: str, payload: dict, level: str = "info"):
        """Send a v2 typed message over WebSocket."""
        try:
            await ws.send_json({
                "v": 2,
                "id": str(uuid.uuid4()),
                "type": msg_type,
                "level": level,
                "session": app.state.current_session,
                "target": app.state.active_target,
                "ts": time.time(),
                "payload": payload,
            })
        except Exception:
            pass  # Connection may be closed


    def verify_token(
        token: Optional[str] = Query(None),
        authorization: Optional[str] = Header(None),
        x_mto_token: Optional[str] = Header(None, alias="X-MTO-Token"),
    ):
        """Verify auth token from header or query parameter.

        Checks in order: Authorization: Bearer <token>, X-MTO-Token header,
        then query parameter (backward compat). Raises 401 if none match.
        """
        if app.state.no_auth:
            return
        expected = app.state.token
        provided = None
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[7:]
        elif x_mto_token:
            provided = x_mto_token
        elif token:
            provided = token
        if not provided or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    # Content Security Policy
    CSP_POLICY = "; ".join([
        "default-src 'self'",
        "script-src 'self' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "connect-src 'self' ws: wss: https://cdn.jsdelivr.net",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "frame-src *",  # dev preview iframes
        "base-uri 'self'",
        "form-action 'self'",
    ])

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        # Don't overwrite CSP if the handler already set a nonce-aware policy
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = CSP_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Prevent aggressive caching of static assets in PWA standalone mode
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    # Dynamic manifest — rewrite paths when base_path is set
    if config.base_path:
        @app.get("/static/manifest.json")
        async def manifest():
            manifest_path = STATIC_DIR / "manifest.json"
            if not manifest_path.exists():
                return HTMLResponse(status_code=404)
            import json as _json
            data = _json.loads(manifest_path.read_text(encoding="utf-8"))
            bp = config.base_path
            data["start_url"] = bp + "/"
            data["scope"] = bp + "/"
            data["id"] = bp
            for icon in data.get("icons", []):
                if icon.get("src", "").startswith("/"):
                    icon["src"] = bp + icon["src"]
            return JSONResponse(data, headers={"Cache-Control": "no-cache, must-revalidate"})

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/favicon.ico")
    async def favicon():
        """Serve favicon from static dir."""
        icon = STATIC_DIR / "apple-touch-icon.png"
        if icon.exists():
            return FileResponse(icon, media_type="image/png")
        return Response(status_code=204)

    @app.get("/sw.js")
    async def service_worker():
        """Serve service worker from root with proper headers.

        When base_path is set, inject __BASE_PATH constant at the top
        so the SW can prefix its hardcoded paths.
        """
        sw_path = STATIC_DIR / "sw.js"
        if not sw_path.exists():
            return HTMLResponse(status_code=404)
        content = sw_path.read_text(encoding="utf-8")
        bp = config.base_path
        content = f'const __BASE_PATH = "{bp}";\n' + content
        return Response(
            content,
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": (bp + "/") if bp else "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    @app.get("/")
    async def index(_auth=Depends(verify_token)):
        """Serve the main HTML page.

        When a built bundle exists (dist/terminal.min.js), rewrite the
        script tag to load it.  Otherwise switch to type="module" so raw
        ES imports in terminal.js work without a build step.

        When base_path is configured, inject a fetch monkey-patch so all
        84+ fetch() calls automatically prefix the reverse proxy path.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        dist_js = STATIC_DIR / "dist" / "terminal.min.js"
        if dist_js.exists():
            # Preserve version query string for cache busting
            m = re.search(r'terminal\.js\?v=(\d+)', html)
            v_qs = f"?v={m.group(1)}" if m else ""
            html = re.sub(
                r'<script\s+defer\s+src="/static/terminal\.js\?v=\d+"',
                f'<script defer src="/static/dist/terminal.min.js{v_qs}"',
                html,
                count=1,
            )
        else:
            html = re.sub(
                r'<script\s+defer\s+src="/static/terminal\.js\?v=\d+"',
                '<script type="module" src="/static/terminal.js"',
                html,
                count=1,
            )

        base_path = config.base_path
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}

        if base_path:
            # Rewrite static asset references
            html = html.replace('href="/static/', f'href="{base_path}/static/')
            html = html.replace('src="/static/', f'src="{base_path}/static/')

            # Inject fetch monkey-patch with CSP nonce
            nonce = secrets.token_urlsafe(16)
            patch_script = (
                f'<script nonce="{nonce}">'
                f'window.__BASE_PATH="{base_path}";'
                "(function(){var f=window.fetch;window.fetch=function(u,o){"
                'if(typeof u==="string"&&u.startsWith("/"))u=window.__BASE_PATH+u;'
                "return f.call(this,u,o)};})();"
                "</script>"
            )
            html = html.replace(
                '<meta charset="UTF-8">',
                f'<meta charset="UTF-8">\n    {patch_script}',
            )

            # Add nonce to CSP for this response
            csp_with_nonce = CSP_POLICY.replace(
                "script-src 'self'",
                f"script-src 'self' 'nonce-{nonce}'",
            )
            headers["Content-Security-Policy"] = csp_with_nonce

        return HTMLResponse(html, headers=headers)

    @app.get("/config")
    async def get_config(request: Request, _auth=Depends(verify_token)):
        """Return client configuration as JSON, with per-device overrides."""
        result = app.state.config.to_dict()
        device = _resolve_device(request, app.state.config.devices)
        if device:
            if device.font_size is not None:
                result["font_size"] = device.font_size
            result["physical_kb"] = device.physical_kb
        # Add driver identity for frontend
        result["agent_type"] = app.state.driver.id()
        result["agent_name"] = app.state.driver.display_name()
        return result

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok", "version": "0.2.0"}

    @app.get("/api/ws-debug")
    async def ws_debug():
        """Debug endpoint — returns WebSocket handler state."""
        return {
            "active_ws": app.state.active_client is not None,
            "ws_lock_locked": app.state.ws_connect_lock.locked(),
            "last_ws_connect": round(app.state.last_ws_connect, 2),
            "seconds_ago": round(time.time() - app.state.last_ws_connect, 1) if app.state.last_ws_connect else None,
            "read_task": app.state.read_task is not None and not app.state.read_task.done() if app.state.read_task else False,
        }

    @app.get("/api/setup-status")
    async def setup_status(_auth=Depends(verify_token)):
        """Return tmux auto-setup status and result."""
        return {
            "auto_setup": config.auto_setup,
            "result": app.state.setup_result,
        }

    @app.get("/api/tmux/sessions")
    async def get_tmux_sessions(
        _auth=Depends(verify_token),
        prefix: str = Query(""),
    ):
        """
        List available tmux sessions.
        Optionally filter by prefix (e.g., 'claude-' for Claude sessions).
        """

        sessions = list_tmux_sessions(prefix)
        return {
            "sessions": sessions,
            "current": app.state.current_session,
            "prefix": prefix,
        }

    @app.get("/api/targets")
    async def list_targets(_auth=Depends(verify_token)):
        """
        List all panes/windows in the current session with their working directories.
        Used for explicit target selection when working with multiple projects.
        """

        session = app.state.current_session
        targets = []

        try:
            # tmux list-panes -s lists all panes in session
            # Format: window_index:pane_index|pane_current_path|window_name|pane_id|pane_title
            pane_fmt = "#{window_index}:#{pane_index}|#{pane_current_path}|#{window_name}|#{pane_id}|#{pane_title}"
            raw = await app.state.runtime.list_panes(session, fmt=pane_fmt)

            if raw:
                seen_cwds = {}  # Track duplicate cwds
                for line in raw.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 5:
                            cwd = Path(parts[1])
                            cwd_str = str(cwd)
                            target_id = parts[0]  # "0:0" (window:pane)
                            pane_title = parts[4] if parts[4] else None

                            # Track duplicates
                            if cwd_str in seen_cwds:
                                seen_cwds[cwd_str].append(target_id)
                            else:
                                seen_cwds[cwd_str] = [target_id]

                            # Detect team role from window name
                            window_name = parts[2]
                            team_role = None
                            agent_name = None
                            if window_name == "leader":
                                team_role = "leader"
                                agent_name = "leader"
                            elif window_name.startswith("a-"):
                                team_role = "agent"
                                agent_name = window_name

                            targets.append({
                                "id": target_id,
                                "pane_id": parts[3],  # "%0"
                                "cwd": cwd_str,
                                "window_name": window_name,
                                "window_index": parts[0].split(":")[0],
                                "pane_title": pane_title,
                                "project": cwd.name,  # Last component of path
                                "is_active": target_id == app.state.active_target,
                                "team_role": team_role,
                                "agent_name": agent_name,
                            })

                # Mark targets with duplicate cwds
                for target in targets:
                    target["has_duplicate_cwd"] = len(seen_cwds.get(target["cwd"], [])) > 1

        except subprocess.TimeoutExpired:
            logger.error("Timeout listing tmux panes")
        except Exception as e:
            logger.error(f"Error listing targets: {e}")

        # Check if active target still exists
        active_exists = any(t["id"] == app.state.active_target for t in targets)

        # Get current path resolution info
        path_info = get_repo_path_info()

        # Check if session has multiple distinct projects
        unique_cwds = set(t["cwd"] for t in targets)
        multi_project = len(unique_cwds) > 1

        has_team = any(t.get("team_role") for t in targets)

        return {
            "targets": targets,
            "active": app.state.active_target,
            "active_exists": active_exists,
            "session": session,
            "multi_project": multi_project,
            "unique_projects": len(unique_cwds),
            "has_team": has_team,
            "resolution": {
                "path": str(path_info["path"]) if path_info["path"] else None,
                "source": path_info["source"],
                "is_fallback": path_info["is_fallback"],
                "warning": path_info["warning"]
            }
        }

    @app.post("/api/target/select")
    async def select_target(
        target_id: str = Query(...),
        _auth=Depends(verify_token)
    ):
        """Set the active target pane for repo operations."""
        _start_total = time.time()
        logger.info(f"[TIMING] /api/target/select START target_id={target_id}")


        session = app.state.current_session

        # Verify target exists
        try:
            _t1 = time.time()
            raw = await app.state.runtime.list_panes(session, fmt="#{window_index}:#{pane_index}")
            logger.info(f"[TIMING] list-panes took {time.time()-_t1:.3f}s")
            valid_targets = raw.strip().split("\n") if raw else []

            if target_id not in valid_targets:
                return JSONResponse({
                    "error": "Target pane not found",
                    "target_id": target_id,
                    "valid_targets": valid_targets
                }, status_code=409)

        except Exception as e:
            logger.error(f"Error verifying target: {e}")
            # Allow selection even if verification fails
            pass

        # Clear old target's log mapping if not pinned (force re-detection)
        old_target = app.state.active_target
        if old_target and old_target in app.state.target_log_mapping:
            old_mapping = app.state.target_log_mapping[old_target]
            if not (isinstance(old_mapping, dict) and old_mapping.get("pinned")):
                del app.state.target_log_mapping[old_target]

        # Skip full switch if already on this target (avoids WS disconnect on initial sync)
        if app.state.active_target == target_id:
            logger.info(f"Target {target_id} already active, skipping switch")
            return {
                "success": True,
                "active": target_id,
                "pane_id": target_id,
                "epoch": app.state.target_epoch,
                "verified": True
            }

        app.state.active_target = target_id
        app.state.audit_log.log("target_select", {"target": target_id})

        # Persist active target for restart recovery
        try:
            state_file = Path.home() / ".cache" / "mobile-overlay" / "active_target.txt"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(target_id)
        except Exception:
            pass

        # Actually switch tmux to the selected pane so the PTY shows it
        switch_verified = False
        try:
            # Parse target_id (format: "window:pane" like "0:1")
            parts = target_id.split(":")
            if len(parts) == 2:
                window_idx, pane_idx = parts
                # Switch to the window first
                _t2 = time.time()
                await app.state.runtime.select_window(f"{session}:{window_idx}")
                logger.info(f"[TIMING] select-window took {time.time()-_t2:.3f}s")
                # Then select the pane within that window (format: session:window.pane)
                _t3 = time.time()
                await app.state.runtime.select_pane(f"{session}:{window_idx}.{pane_idx}")
                logger.info(f"[TIMING] select-pane took {time.time()-_t3:.3f}s")
                logger.info(f"Switched tmux to pane {target_id}")

                # Verify switch completed (max 1s total, not per-iteration)
                _t4 = time.time()
                _verify_iterations = 0
                _verify_deadline = _t4 + 1.0  # Hard cap at 1 second total
                while time.time() < _verify_deadline:
                    _verify_iterations += 1
                    try:
                        current = await app.state.runtime.display_message(
                            session, "#{window_index}:#{pane_index}"
                        )
                        if current == target_id:
                            switch_verified = True
                            break
                    except Exception:
                        pass  # Continue loop, will exit via deadline
                    await asyncio.sleep(0.05)
                logger.info(f"[TIMING] verify loop took {time.time()-_t4:.3f}s ({_verify_iterations} iterations)")

                if not switch_verified:
                    logger.warning(f"Target switch verification failed for {target_id}")
        except Exception as e:
            logger.warning(f"Failed to switch tmux pane: {e}")

        # Increment epoch and clear output buffer on verified switch
        app.state.target_epoch += 1
        app.state.output_buffer.clear()
        logger.info(f"Target switch epoch={app.state.target_epoch}, buffer cleared")

        # PTY stays alive — it's attached to the tmux session, not a specific pane.
        # select-window + select-pane already changed what the PTY displays.
        # No need to kill PTY or close WebSocket; the stream naturally shows the new pane.

        # Start background file monitor to detect which log file this target uses
        asyncio.create_task(app.state._monitor_log_file_for_target(target_id))

        logger.info(f"[TIMING] /api/target/select TOTAL took {time.time()-_start_total:.3f}s")
        return {
            "success": True,
            "active": target_id,
            "pane_id": target_id,
            "epoch": app.state.target_epoch,
            "verified": switch_verified
        }

    @app.post("/api/window/new")
    async def create_new_window(
        request: Request,
        _auth=Depends(verify_token)
    ):
        """
        Create a new tmux window in a repo's configured session.

        JSON body:
          - repo_label: Label of repo from config (use this OR path)
          - path: Absolute path to directory under a workspace_dir (use this OR repo_label)
          - window_name: (optional) Name for the new window
          - auto_start_agent: (optional, default false) Start agent after creating window
        """

        # Parse JSON body
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        repo_label = body.get("repo_label")
        dir_path = body.get("path")
        window_name = body.get("window_name", "")
        auto_start_agent = body.get("auto_start_agent", False)

        repo = None  # Set when using repo_label flow

        if repo_label:
            # --- Existing repo-based flow ---
            repo = next((r for r in config.repos if r.label == repo_label), None)
            if not repo:
                return JSONResponse({
                    "error": f"Unknown repo: {repo_label}",
                    "available_repos": [r.label for r in config.repos]
                }, status_code=404)

            repo_path = Path(repo.path)
            if not repo_path.exists():
                return JSONResponse({
                    "error": f"Repo path does not exist: {repo.path}"
                }, status_code=400)

            session = repo.session
            resolved_path = str(repo_path.resolve())

        elif dir_path:
            # --- Workspace directory flow ---
            # Validate path is under one of the configured workspace_dirs
            target = Path(dir_path).resolve()
            allowed = False
            ws_dirs = config.workspace_dirs or [str(Path.home() / "dev")]
            for ws_dir in ws_dirs:
                ws_resolved = Path(ws_dir).expanduser().resolve()
                try:
                    target.relative_to(ws_resolved)
                    allowed = True
                    break
                except ValueError:
                    continue

            if not allowed:
                return JSONResponse({
                    "error": "Path is not under any configured workspace_dir"
                }, status_code=403)

            if not target.is_dir():
                return JSONResponse({
                    "error": f"Path does not exist or is not a directory: {dir_path}"
                }, status_code=400)

            session = app.state.current_session
            resolved_path = str(target)

        else:
            return JSONResponse({
                "error": "Either repo_label or path is required"
            }, status_code=400)

        # Sanitize window name: only allow [a-zA-Z0-9_.-], max 50 chars
        if window_name:
            sanitized_name = re.sub(r'[^a-zA-Z0-9_.-]', '', window_name)[:50]
        else:
            sanitized_name = ""

        # If sanitized name is empty, use directory basename
        if not sanitized_name:
            dir_basename = Path(resolved_path).name
            sanitized_name = re.sub(r'[^a-zA-Z0-9_.-]', '', dir_basename)[:50]
            if not sanitized_name and repo_label:
                sanitized_name = re.sub(r'[^a-zA-Z0-9_.-]', '', repo_label)[:50]
            if not sanitized_name:
                sanitized_name = "window"

        # Add random suffix to handle name collisions
        final_name = f"{sanitized_name}-{secrets.token_hex(2)}"

        try:
            win_info = _create_tmux_window(session, final_name, resolved_path)
            target_id = win_info["target_id"]
            pane_id = win_info.get("pane_id")

            # Audit log the action
            app.state.audit_log.log("window_create", {
                "repo_label": repo_label or Path(resolved_path).name,
                "session": session,
                "window_name": final_name,
                "target_id": target_id,
                "pane_id": pane_id,
                "path": resolved_path,
                "auto_start_agent": auto_start_agent
            })

            logger.info(f"Created window '{final_name}' in session '{session}' at {resolved_path}")

            # If auto_start_agent, send startup command after configured delay
            if auto_start_agent and pane_id:
                # Get startup command from repo config (if repo flow), default to "claude"
                startup_cmd = (repo.startup_command if repo else None) or app.state.driver.start_command()[0]

                # Validate startup command
                if "\n" in startup_cmd or "\r" in startup_cmd:
                    return JSONResponse({
                        "error": "startup_command cannot contain newlines"
                    }, status_code=400)
                if len(startup_cmd) > 200:
                    return JSONResponse({
                        "error": "startup_command exceeds 200 character limit"
                    }, status_code=400)

                startup_delay = (repo.startup_delay_ms if repo else 300) / 1000.0
                audit_label = repo_label or Path(resolved_path).name

                async def _send_and_audit():
                    await _send_startup_command(pane_id, startup_cmd, startup_delay)
                    app.state.audit_log.log("startup_command_exec", {
                        "pane_id": pane_id,
                        "command": startup_cmd,
                        "repo_label": audit_label
                    })

                asyncio.create_task(_send_and_audit())

            return {
                "success": True,
                "target_id": target_id,
                "pane_id": pane_id,
                "window_name": final_name,
                "session": session,
                "repo_label": repo_label,
                "path": resolved_path,
                "auto_start_agent": auto_start_agent
            }

        except RuntimeError as e:
            error_msg = str(e)
            if "can't find" in error_msg.lower() or "no such session" in error_msg.lower():
                return JSONResponse({
                    "error": f"Session '{session}' not found. Create it first with: tmux new -s {session}"
                }, status_code=400)
            return JSONResponse({"error": error_msg}, status_code=500)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout creating window"}, status_code=504)
        except Exception as e:
            logger.error(f"Error creating window: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/pane/kill")
    async def kill_pane(
        request: Request,
        _auth=Depends(verify_token)
    ):
        """
        Kill a tmux pane. Cannot kill the currently active pane.

        JSON body:
          - target_id: Pane target in "window:pane" format (e.g. "2:0")
        """

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        target_id = body.get("target_id")
        if not target_id or not isinstance(target_id, str):
            return JSONResponse({"error": "target_id is required"}, status_code=400)

        # Validate format: "window:pane"
        parts = target_id.split(":")
        if len(parts) != 2 or not all(p.strip() for p in parts):
            return JSONResponse({"error": "Invalid target_id format, expected 'window:pane'"}, status_code=400)

        # Cannot kill the active pane
        if target_id == app.state.active_target:
            return JSONResponse({"error": "Cannot kill the active pane"}, status_code=400)

        session = app.state.current_session
        tmux_target = get_tmux_target(session, target_id)

        # Verify the pane exists
        try:
            check = await run_subprocess(
                ["tmux", "list-panes", "-t", tmux_target],
                capture_output=True, text=True, timeout=5
            )
            if check.returncode != 0:
                return JSONResponse({"error": f"Pane {target_id} not found"}, status_code=404)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout checking pane"}, status_code=504)

        # Kill the pane
        try:
            result = await run_subprocess(
                ["tmux", "kill-pane", "-t", tmux_target],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return JSONResponse({"error": f"Failed to kill pane: {result.stderr.strip()}"}, status_code=500)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout killing pane"}, status_code=504)

        app.state.audit_log.log("pane_kill", {
            "target_id": target_id,
            "session": session,
        })

        logger.info(f"Killed pane {target_id} in session '{session}'")
        return {"success": True, "killed": target_id}

    @app.get("/api/refresh")
    async def refresh_terminal(
        _auth=Depends(verify_token),
        cols: Optional[int] = Query(None),
        rows: Optional[int] = Query(None),
    ):
        """
        Get current terminal snapshot for refresh.
        If cols/rows provided, resizes tmux pane first to fix garbled output.
        Uses capture-pane with visible content only.
        """

        try:
            session_name = app.state.current_session

            # Use active target pane if set, otherwise fall back to session
            target = get_tmux_target(session_name, app.state.active_target)

            # Resize tmux pane if dimensions provided (fixes garbled output)
            resized = False
            if cols and rows:
                resize_result = await run_subprocess(
                    ["tmux", "resize-pane", "-t", target, "-x", str(cols), "-y", str(rows)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if resize_result.returncode != 0:
                    logger.warning(f"tmux resize-pane failed: {resize_result.stderr}")
                else:
                    logger.info(f"Resized tmux pane {target} to {cols}x{rows}")
                    resized = True

                    # Send Ctrl+L to force screen redraw after resize
                    await run_subprocess(
                        ["tmux", "send-keys", "-t", target, "C-l"],
                        capture_output=True,
                        timeout=1,
                    )
                    # Small delay for redraw to complete
                    await asyncio.sleep(0.15)

            # Capture visible area only (not scrollback) to avoid stale wrapped content
            # Use -S - to start from visible area, or omit -S for default
            result = await run_subprocess(
                ["tmux", "capture-pane", "-p", "-t", target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return JSONResponse(
                    {"error": f"tmux capture-pane failed: {result.stderr}"},
                    status_code=500,
                )
            return {"content": result.stdout, "session": session_name, "target": target}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Refresh timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Refresh error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/restart")
    async def restart_server(request: Request, _auth=Depends(verify_token)):
        """
        Trigger a safe server restart without affecting tmux/Claude sessions.

        - Debounced: 429 if restarted within last 30 seconds
        - Tries systemd first, falls back to execv
        - Returns 202 immediately, restart happens after response flushes
        """

        # Debounce: prevent restart loops
        RESTART_COOLDOWN = 30  # seconds
        now = time.time()
        time_since_last = now - app.state.last_restart_time
        if time_since_last < RESTART_COOLDOWN:
            retry_after = int(RESTART_COOLDOWN - time_since_last) + 1
            logger.warning(f"Restart request rejected: cooldown ({retry_after}s remaining)")
            return JSONResponse(
                {"error": "Restart too soon", "retry_after": retry_after},
                status_code=429,
            )

        # Get client info for logging
        client_ip = "unknown"
        if request.client:
            client_ip = request.client.host

        app.state.last_restart_time = now
        logger.info(f"Restart requested by {client_ip}")

        async def do_restart():
            """Perform restart after response flushes."""
            await asyncio.sleep(0.3)  # Let response flush

            # Try systemd first (check both service names)
            for svc in ["mto.service", "mobile-terminal.service"]:
                try:
                    result = await run_subprocess(
                        ["systemctl", "--user", "is-active", svc],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if result.returncode == 0:
                        logger.info(f"Restarting via systemd {svc} (requested by {client_ip})")
                        subprocess.Popen(
                            ["systemctl", "--user", "restart", svc],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        return
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass

            # Fallback: execv (replaces process in-place)
            # Note: Not compatible with uvicorn --reload or multiple workers
            logger.info(f"Restarting via execv (requested by {client_ip})")
            os.execv(sys.executable, [sys.executable] + sys.argv)

        # Schedule restart in background
        asyncio.create_task(do_restart())

        return JSONResponse({"status": "restarting"}, status_code=202)

    @app.post("/share")
    async def receive_share(request: Request):
        """
        Web Share Target endpoint. Receives shared text/files from mobile OS.
        Stores content and redirects to the app with share params.
        """
        from fastapi.responses import RedirectResponse
        import uuid

        form = await request.form()
        title = form.get("title", "")
        text = form.get("text", "")
        url = form.get("url", "")
        files = form.getlist("files")

        # Combine shared text
        shared_text = " ".join(filter(None, [str(title), str(text), str(url)])).strip()

        # Save files if any
        saved_paths = []
        share_dir = Path.home() / ".cache" / "mobile-overlay" / "shares"
        share_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if hasattr(f, 'filename') and f.filename:
                dest = share_dir / f"{uuid.uuid4().hex[:8]}_{f.filename}"
                content = await f.read()
                dest.write_bytes(content)
                saved_paths.append(str(dest))
                logger.info(f"Share: saved file {f.filename} -> {dest}")

        if saved_paths:
            shared_text += " " + " ".join(saved_paths)

        logger.info(f"Share received: text={shared_text[:100]}")

        # Store in session for the client to pick up
        share_id = uuid.uuid4().hex[:12]
        if not hasattr(app.state, '_pending_shares'):
            app.state._pending_shares = {}
        app.state._pending_shares[share_id] = {
            "text": shared_text,
            "files": saved_paths,
            "ts": time.time(),
        }

        # Redirect to app with share ID
        base = config.base_path or ""
        return RedirectResponse(f"{base}/?share={share_id}", status_code=303)

    @app.get("/api/share/pending")
    async def get_pending_share(
        share_id: str = Query(...),
        _auth=Depends(verify_token),
    ):
        """Retrieve a pending share by ID."""
        shares = getattr(app.state, '_pending_shares', {})
        share = shares.pop(share_id, None)
        if not share:
            return {"found": False}
        return {"found": True, **share}

    @app.post("/api/reload-env")
    async def reload_env(_auth=Depends(verify_token)):
        """
        Reload environment variables from .env file.
        Useful for updating API keys without full server restart.
        """

        try:
            from dotenv import load_dotenv
            # override=True replaces existing env vars with .env values
            loaded = load_dotenv(override=True)
            if loaded:
                logger.info("Reloaded .env file")
                return {"status": "reloaded", "message": "Environment variables refreshed from .env"}
            else:
                return {"status": "no_file", "message": "No .env file found (env unchanged)"}
        except ImportError:
            return JSONResponse(
                {"error": "python-dotenv not installed"},
                status_code=500,
            )
        except Exception as e:
            logger.error(f"Failed to reload .env: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    def get_repo_path_info() -> dict:
        """
        Get repo path with resolution details.
        Returns: {path, source, target_id, is_fallback, warning}
        """
        session_name = app.state.current_session
        result_info = {
            "path": None,
            "source": None,
            "target_id": app.state.active_target,
            "is_fallback": False,
            "warning": None
        }

        # Priority 1: Explicit target selection
        if app.state.active_target:
            try:
                target = get_tmux_target(session_name, app.state.active_target)
                result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", target,
                     "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0 and result.stdout.strip():
                    target_path = Path(result.stdout.strip())
                    if target_path.exists():
                        result_info["path"] = target_path
                        result_info["source"] = "explicit_target"
                        return result_info
                    else:
                        result_info["warning"] = f"Target path does not exist: {target_path}"
                else:
                    result_info["warning"] = f"Target pane not found: {app.state.active_target}"
            except Exception as e:
                result_info["warning"] = f"Error resolving target: {e}"

            # Target was set but failed - this is a fallback situation
            result_info["is_fallback"] = True

        # Priority 2: Check if session matches a configured repo
        for repo in config.repos:
            if repo.session == session_name:
                result_info["path"] = Path(repo.path)
                result_info["source"] = "configured_repo"
                if not app.state.active_target:
                    result_info["is_fallback"] = True
                return result_info

        # Priority 3: Fall back to project_root if set
        if config.project_root:
            result_info["path"] = config.project_root
            result_info["source"] = "project_root"
            result_info["is_fallback"] = True
            return result_info

        # Priority 4: Query tmux for active pane's working directory
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", session_name, "#{pane_current_path}"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0 and result.stdout.strip():
                pane_path = Path(result.stdout.strip())
                if pane_path.exists():
                    result_info["path"] = pane_path
                    result_info["source"] = "active_pane_cwd"
                    result_info["is_fallback"] = True
                    return result_info
        except Exception:
            pass

        # Last resort: server's working directory
        result_info["path"] = Path.cwd()
        result_info["source"] = "server_cwd"
        result_info["is_fallback"] = True
        result_info["warning"] = "Using server working directory (no target selected)"
        return result_info

    def get_current_repo_path() -> Optional[Path]:
        """Get the path of the current repo based on session name and target."""
        return get_repo_path_info()["path"]

    def validate_target(session: Optional[str], pane_id: Optional[str]) -> dict:
        """
        Validate that the client's session and pane_id match server state.
        Returns dict with 'valid' bool and 'error' message if invalid.

        Used to prevent state-changing operations on wrong target.
        """
        result = {"valid": True, "error": None, "expected": {}, "received": {}}

        expected_session = app.state.current_session
        expected_pane = app.state.active_target

        result["expected"] = {"session": expected_session, "pane_id": expected_pane}
        result["received"] = {"session": session, "pane_id": pane_id}

        # Validate session
        if session and session != expected_session:
            result["valid"] = False
            result["error"] = f"Session mismatch: expected '{expected_session}', got '{session}'"
            return result

        # Validate pane_id (only if server has an active target set)
        if expected_pane and pane_id and pane_id != expected_pane:
            result["valid"] = False
            result["error"] = f"Target mismatch: expected '{expected_pane}', got '{pane_id}'"
            return result

        # If pane_id provided, verify it exists in current session
        if pane_id:
            try:
                check = subprocess.run(
                    ["tmux", "list-panes", "-s", "-t", expected_session,
                     "-F", "#{window_index}:#{pane_index}"],
                    capture_output=True, text=True, timeout=2
                )
                valid_panes = check.stdout.strip().split("\n") if check.returncode == 0 else []
                if pane_id not in valid_panes:
                    result["valid"] = False
                    result["error"] = f"Pane '{pane_id}' not found in session '{expected_session}'"
                    return result
            except Exception as e:
                logger.warning(f"Could not verify pane: {e}")

        return result

    def _read_claude_file(filename: str, label: str):
        """Read a file from the current repo's .claude/ directory."""
        repo_path = get_current_repo_path()
        if not repo_path:
            return {"exists": False, "content": "", "session": app.state.current_session}
        target_file = repo_path / ".claude" / filename
        if not target_file.exists():
            return {"exists": False, "content": "", "path": str(target_file),
                    "session": app.state.current_session}
        try:
            content = target_file.read_text(errors="replace")
            return {"exists": True, "content": content, "path": str(target_file),
                    "session": app.state.current_session,
                    "modified": target_file.stat().st_mtime}
        except Exception as e:
            logger.error(f"Error reading {label}: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)


    @app.get("/current-session")
    async def get_current_session(_auth=Depends(verify_token)):
        """Return current session name."""
        return {"session": app.state.current_session}

    @app.post("/switch-repo")
    async def switch_repo(session: str = Query(...), _auth=Depends(verify_token)):
        """
        Switch to a different tmux session (repo).

        This closes the current pty and prepares for a new connection.
        The client should reconnect the WebSocket after this call.
        """

        # Validate session is in configured repos (or is the default session)
        valid_sessions = [r.session for r in config.repos] + [config.session_name]
        if session not in valid_sessions:
            return JSONResponse({"error": f"Unknown session: {session}"}, status_code=400)

        # Close current WebSocket connection
        if app.state.active_client is not None:
            try:
                await app.state.active_client.close(code=4003)  # 4003 = switching repos
            except Exception:
                pass
            app.state.active_client = None

        # Cancel read task
        if app.state.read_task is not None:
            app.state.read_task.cancel()
            app.state.read_task = None

        # Kill child process and close pty
        if app.state.runtime.has_fd:
            try:
                app.state.runtime.terminate()
            except Exception:
                pass
            app.state.runtime.close_fd()

        # Clear output buffer (don't replay old session's content)
        app.state.output_buffer.clear()

        # Clear target selection and log mappings (pane IDs are session-specific)
        app.state.active_target = None
        app.state.target_log_mapping.clear()

        # Update current session
        app.state.current_session = session
        logger.info(f"Switched to session: {session}")

        return {"status": "ok", "session": session}

    def _build_observe_context_sync(pane_id: str) -> Optional[ObserveContext]:
        """Build ObserveContext from a pane_id with one tmux call (sync)."""
        session = app.state.current_session
        try:
            tmux_target = get_tmux_target(session, pane_id)
            result = subprocess.run(
                ["tmux", "display-message", "-t", tmux_target, "-p",
                 "#{pane_pid}|#{pane_title}|#{pane_current_path}"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode != 0:
                return None
            output = result.stdout.strip()
            parts = output.split("|", 2)
            shell_pid = int(parts[0]) if parts[0].isdigit() else None
            pane_title = parts[1] if len(parts) > 1 else ""
            cwd = parts[2] if len(parts) > 2 else ""
            repo_path = Path(cwd) if cwd and Path(cwd).exists() else get_current_repo_path()
            return ObserveContext(
                session_name=session,
                target=pane_id or "",
                tmux_target=tmux_target,
                shell_pid=shell_pid,
                pane_title=pane_title,
                repo_path=repo_path,
            )
        except Exception as e:
            logger.debug(f"Error building observe context: {e}")
            return None

    async def _build_observe_context(pane_id: str) -> Optional[ObserveContext]:
        """Async wrapper — runs tmux subprocess off the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _build_observe_context_sync, pane_id)

    # ===== Phase Detection — delegated to app.state.driver.observe() =====
    _git_head_cache: dict = {"value": "", "ts": 0.0}

    def _get_git_head() -> str:
        """Get short git HEAD hash, cached for 10s."""
        now = time.time()
        if now - _git_head_cache["ts"] < 10 and _git_head_cache["value"]:
            return _git_head_cache["value"]
        try:
            repo_path = get_current_repo_path()
            if not repo_path:
                return ""
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2,
                cwd=str(repo_path),
            )
            if result.returncode == 0:
                val = result.stdout.strip()
                _git_head_cache.update({"value": val, "ts": now})
                return val
        except Exception:
            pass
        return ""

    def _try_auto_snapshot(session: str, target: str, phase_result: dict):
        """Auto-capture a minimal snapshot from push_monitor (rate-limited by caller)."""
        tool = phase_result.get("tool", "")
        phase = phase_result.get("phase", "")

        # Determine label from tool
        label_map = {
            "Edit": "edit", "Write": "edit", "NotebookEdit": "edit",
            "Bash": "bash",
            "EnterPlanMode": "plan_transition", "ExitPlanMode": "plan_transition",
            "Task": "task",
            "AskUserQuestion": "tool_call",
        }
        label = label_map.get(tool, "tool_call")

        # Find current log file info
        repo_path = get_current_repo_path()
        if not repo_path:
            return
        project_id = get_project_id(repo_path)
        cpd = Path.home() / ".claude" / "projects" / project_id
        if not cpd.exists():
            return
        jf = list(cpd.glob("*.jsonl"))
        if not jf:
            return
        lf = max(jf, key=lambda f: f.stat().st_mtime)

        ts = int(time.time() * 1000)
        snap_id = f"snap_{ts}"
        snapshot = {
            "id": snap_id,
            "timestamp": ts,
            "session": session,
            "pane_id": target or "",
            "label": label,
            "log_offset": lf.stat().st_size,
            "log_path": str(lf),
            "git_head": _get_git_head(),
            "terminal_text": "",  # Empty by default, load on demand
            "log_entries": "",    # Empty by default, load on demand
            "note": "",
            "image_path": None,
            "pinned": False,
        }

        # Use existing SnapshotBuffer (keyed by session:pane_id)
        snap_key = f"{session}:{target}" if target else session
        buf = app.state.snapshot_buffer
        with buf._lock:
            if snap_key not in buf._snapshots:
                buf._snapshots[snap_key] = OrderedDict()
            buf._snapshots[snap_key][snap_id] = snapshot
            while len(buf._snapshots[snap_key]) > buf.MAX_SNAPSHOTS:
                evicted = False
                for key in list(buf._snapshots[snap_key].keys()):
                    if not buf._snapshots[snap_key][key].get("pinned"):
                        del buf._snapshots[snap_key][key]
                        evicted = True
                        break
                if not evicted:
                    break

    # _detect_phase and _detect_phase_for_cwd deleted — logic moved to drivers/claude.py
    _git_info_cache: dict = {}    # key: cwd_str -> {result, ts}

    def _get_git_info(cwd: Path) -> dict:
        """Get branch name and worktree status for a pane's cwd."""
        git_info = {"branch": None, "is_worktree": False, "is_main": False}
        try:
            toplevel_result = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=2
            )
            if toplevel_result.returncode != 0:
                return git_info
            repo_root = Path(toplevel_result.stdout.strip())

            branch_result = subprocess.run(
                ["git", "-C", str(cwd), "branch", "--show-current"],
                capture_output=True, text=True, timeout=2
            )
            if branch_result.returncode == 0:
                branch = branch_result.stdout.strip()
                if branch:
                    git_info["branch"] = branch
                else:
                    # Detached HEAD fallback: show short commit hash
                    head_result = subprocess.run(
                        ["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=2
                    )
                    if head_result.returncode == 0:
                        git_info["branch"] = f"({head_result.stdout.strip()})"
                git_info["is_main"] = git_info["branch"] in ("main", "master")

            # Detect worktree: .git at repo root is a file (not dir) for worktrees
            dot_git = repo_root / ".git"
            git_info["is_worktree"] = dot_git.is_file()
        except Exception:
            pass
        return git_info

    def _get_git_info_cached(cwd: Path) -> dict:
        """Get git info with 10s cache per cwd."""
        cwd_str = str(cwd)
        cached = _git_info_cache.get(cwd_str)
        if cached and time.time() - cached["ts"] < 10:
            return cached["result"]
        result = _get_git_info(cwd)
        _git_info_cache[cwd_str] = {"result": result, "ts": time.time()}
        if len(_git_info_cache) > 30:
            now = time.time()
            stale = [k for k, v in _git_info_cache.items() if now - v["ts"] > 60]
            for k in stale:
                del _git_info_cache[k]
        return result

    # --- Router registration ---
    from mobile_terminal.routers import AppDeps
    from mobile_terminal.routers import context as context_router
    from mobile_terminal.routers import files as files_router
    from mobile_terminal.routers import challenge as challenge_router
    from mobile_terminal.routers import runner as runner_router
    from mobile_terminal.routers import preview as preview_router
    from mobile_terminal.routers import snapshots as snapshots_router
    from mobile_terminal.routers import queue as queue_router
    from mobile_terminal.routers import git as git_router
    from mobile_terminal.routers import mcp as mcp_router
    from mobile_terminal.routers import env as env_router
    from mobile_terminal.routers import logs as logs_router
    from mobile_terminal.routers import process as process_router
    from mobile_terminal.routers import team as team_router
    from mobile_terminal.routers import team_launcher as team_launcher_router
    from mobile_terminal.routers import push as push_router
    from mobile_terminal.routers import terminal_io as terminal_io_router
    from mobile_terminal.routers import terminal_sse as terminal_sse_router
    from mobile_terminal.routers import scratch as scratch_router
    from mobile_terminal.routers import backlog as backlog_router
    from mobile_terminal.routers import permissions as permissions_router

    deps = AppDeps(
        verify_token=verify_token,
        send_typed=send_typed,
        get_current_repo_path=get_current_repo_path,
        get_repo_path_info=get_repo_path_info,
        validate_target=validate_target,
        read_claude_file=_read_claude_file,
        build_observe_context=_build_observe_context,
        get_git_head=_get_git_head,
        get_git_info_cached=_get_git_info_cached,
        try_auto_snapshot=_try_auto_snapshot,
    )
    context_router.register(app, deps)
    files_router.register(app, deps)
    challenge_router.register(app, deps)
    runner_router.register(app, deps)
    preview_router.register(app, deps)
    snapshots_router.register(app, deps)
    queue_router.register(app, deps)
    git_router.register(app, deps)
    mcp_router.register(app, deps)
    env_router.register(app, deps)
    logs_router.register(app, deps)
    process_router.register(app, deps)
    team_router.register(app, deps)
    team_launcher_router.register(app, deps)
    push_router.register(app, deps)
    terminal_io_router.register(app, deps)
    terminal_sse_router.register(app, deps)
    scratch_router.register(app, deps)
    backlog_router.register(app, deps)
    permissions_router.register(app, deps)

    @app.on_event("startup")
    async def startup():
        """Start input queue and command queue on startup, run auto-setup if enabled."""
        app.state.input_queue.start()
        app.state.command_queue.start()

        # Generate VAPID keys for push notifications
        if config.push_enabled:
            try:
                key_dir = Path.home() / ".mobile-terminal"
                key_dir.mkdir(parents=True, exist_ok=True)
                key_path = key_dir / "vapid_private.pem"
                if not key_path.exists():
                    from py_vapid import Vapid
                    vapid = Vapid()
                    vapid.generate_keys()
                    vapid.save_key(str(key_path))
                    vapid.save_public_key(str(key_dir / "vapid_public.pem"))
                    logger.info("Generated new VAPID keys for push notifications")
                from py_vapid import Vapid
                vapid = Vapid.from_file(str(key_path))
                app.state.vapid_key_path = key_path
                import base64 as _b64
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
                raw_pub = vapid.public_key.public_bytes(
                    encoding=Encoding.X962,
                    format=PublicFormat.UncompressedPoint
                )
                app.state.vapid_public_key = _b64.urlsafe_b64encode(raw_pub).rstrip(b'=').decode('ascii')
                logger.info("VAPID keys loaded for push notifications")
            except ImportError:
                logger.info("pywebpush/py_vapid not installed, push notifications disabled")
                app.state.vapid_key_path = None
                app.state.vapid_public_key = None
            except Exception as e:
                logger.warning(f"VAPID key setup failed: {e}")
                app.state.vapid_key_path = None
                app.state.vapid_public_key = None

        # Auto-setup: create/adopt tmux session with configured repo windows
        if config.auto_setup:
            try:
                setup_result = await ensure_tmux_setup(config)
                app.state.setup_result = setup_result
                if setup_result["created_session"]:
                    logger.info(f"auto_setup: created new session '{config.session_name}'")
                if setup_result["errors"]:
                    for err in setup_result["errors"]:
                        logger.warning(f"auto_setup: {err}")
            except Exception as e:
                logger.error(f"auto_setup: failed: {e}")
                app.state.setup_result = {"error": str(e)}

        print(f"\n{'=' * 60}")
        print(f"Mobile Terminal Overlay v0.2.0")
        print(f"{'=' * 60}")
        print(f"Session: {config.session_name}")
        print(f"Host:    {config.host}")
        if app.state.no_auth:
            print(f"Auth:    DISABLED (--no-auth)")
            url = f"http://localhost:{config.port}{config.base_path}/"
            if config.host == "0.0.0.0":
                print(f"WARNING: Listening on all interfaces without auth!")
                print(f"         Use --no-auth only on trusted networks (e.g. Tailscale)")
        else:
            print(f"Token:   {app.state.token}")
            url = f"http://localhost:{config.port}{config.base_path}/?token={app.state.token}"
        print(f"URL:     {url}")
        print(f"{'=' * 60}\n")

        # Start background push monitor (defined in push router)
        if config.push_enabled and getattr(app.state, 'vapid_key_path', None):
            push_monitor_fn = getattr(app.state, '_push_monitor', None)
            if push_monitor_fn:
                app.state.push_monitor_task = asyncio.create_task(push_monitor_fn())

        # Start multi-pane permission scanner (always, independent of push)
        perm_scanner_fn = getattr(app.state, '_permission_scanner', None)
        if perm_scanner_fn:
            app.state.permission_scanner_task = asyncio.create_task(perm_scanner_fn())

    @app.on_event("shutdown")
    async def shutdown():
        """Cleanup on shutdown."""
        app.state.input_queue.stop()
        app.state.command_queue.stop()

        push_task = getattr(app.state, 'push_monitor_task', None)
        if push_task and not push_task.done():
            push_task.cancel()
        perm_task = getattr(app.state, 'permission_scanner_task', None)
        if perm_task and not perm_task.done():
            perm_task.cancel()

        if app.state.runtime.has_fd:
            app.state.runtime.close_fd()

    return app



# spawn_tmux has been moved to TmuxRuntime.spawn() in runtime.py


