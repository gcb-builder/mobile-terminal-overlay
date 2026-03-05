"""
FastAPI server for Mobile Terminal Overlay.

Provides:
- Static file serving for the web UI
- WebSocket endpoint for terminal I/O
- Token-based authentication
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from mobile_terminal.drivers import get_driver, ClaudePermissionDetector, ObserveContext, Observation
from mobile_terminal.helpers import (
    _ANSI_ESCAPE_RE, strip_ansi, get_project_id, find_utf8_boundary,
    run_subprocess, get_tmux_target, get_bounded_snapshot,
    get_cached_capture, set_cached_capture, CAPTURE_CACHE_TTL,
    get_cached_log, set_cached_log, LOG_CACHE_TTL,
    get_plan_links, save_plan_links, score_plan_for_repo, get_plans_for_repo,
    CLAUDE_SETTINGS_FILE, MCP_NAME_RE, MCP_MAX_ARGS_SIZE,
    PLUGIN_NAME_RE, INSTALLED_PLUGINS_FILE,
    load_claude_settings, save_claude_settings,
    list_tmux_sessions, _tmux_session_exists, _list_session_windows,
    _match_repo_to_window, _get_pane_command, _create_tmux_window,
    _send_startup_command, ensure_tmux_setup,
    _sigchld_handler, _resolve_device,
    STATIC_DIR, LOG_CACHE_DIR, PLAN_LINKS_FILE,
)


from mobile_terminal.models import (
    RingBuffer, SnapshotBuffer, AuditLog, GitOpLock, InputQueue,
    QueueItem, CommandQueue,
    QUEUE_DIR, get_queue_file, load_queue_from_disk, save_queue_to_disk,
)

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
    app.state.master_fd = None
    app.state.child_pid = None
    app.state.active_websocket = None
    app.state.read_task = None
    app.state.current_session = config.session_name  # Track current session
    app.state.last_ws_connect = 0  # Timestamp of last WebSocket connection
    app.state.ws_connect_lock = asyncio.Lock()  # Prevent concurrent connection handling
    app.state.output_buffer = RingBuffer(max_size=2 * 1024 * 1024)  # 2MB scrollback buffer
    app.state.input_queue = InputQueue()  # Serialized input queue with ACKs
    app.state.command_queue = CommandQueue()  # Deferred-send command queue
    app.state.command_queue.set_app(app)
    app.state.snapshot_buffer = SnapshotBuffer()  # Preview snapshots ring buffer
    app.state.audit_log = AuditLog()  # Audit log for rollback operations
    app.state.git_op_lock = GitOpLock()  # Lock for git write operations
    app.state.active_target = None  # Explicit target pane (window:pane like "0:0")
    app.state.target_log_mapping = {}  # Maps pane_id -> {"path": str, "pinned": bool}
    app.state.last_restart_time = 0.0  # Timestamp of last server restart request
    app.state.target_epoch = 0  # Incremented on each target switch for cache invalidation
    app.state.setup_result = None  # Result from ensure_tmux_setup()
    app.state.last_ws_input_time = 0  # Last time mobile client sent input (for desktop activity detection)
    app.state.permission_detector = ClaudePermissionDetector()  # JSONL-based permission prompt detector
    app.state.driver = get_driver(config.agent_type, config.agent_display_name)

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
        "connect-src 'self' ws: wss:",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "frame-src *",  # dev preview iframes
        "base-uri 'self'",
        "form-action 'self'",
    ])

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/sw.js")
    async def service_worker():
        """Serve service worker from root with proper headers."""
        sw_path = STATIC_DIR / "sw.js"
        if sw_path.exists():
            return FileResponse(
                sw_path,
                media_type="application/javascript",
                headers={
                    "Service-Worker-Allowed": "/",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                },
            )
        return HTMLResponse(status_code=404)

    @app.get("/")
    async def index(_auth=Depends(verify_token)):
        """Serve the main HTML page."""
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

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
            result = await run_subprocess(
                ["tmux", "list-panes", "-s", "-t", session,
                 "-F", "#{window_index}:#{pane_index}|#{pane_current_path}|#{window_name}|#{pane_id}|#{pane_title}"],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0:
                seen_cwds = {}  # Track duplicate cwds
                for line in result.stdout.strip().split("\n"):
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
            result = await run_subprocess(
                ["tmux", "list-panes", "-s", "-t", session,
                 "-F", "#{window_index}:#{pane_index}"],
                capture_output=True, text=True, timeout=5
            )
            logger.info(f"[TIMING] list-panes took {time.time()-_t1:.3f}s")
            valid_targets = result.stdout.strip().split("\n") if result.returncode == 0 else []

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

        app.state.active_target = target_id
        app.state.audit_log.log("target_select", {"target": target_id})

        # Actually switch tmux to the selected pane so the PTY shows it
        switch_verified = False
        try:
            # Parse target_id (format: "window:pane" like "0:1")
            parts = target_id.split(":")
            if len(parts) == 2:
                window_idx, pane_idx = parts
                # Switch to the window first
                _t2 = time.time()
                await run_subprocess(
                    ["tmux", "select-window", "-t", f"{session}:{window_idx}"],
                    capture_output=True, timeout=2
                )
                logger.info(f"[TIMING] select-window took {time.time()-_t2:.3f}s")
                # Then select the pane within that window (format: session:window.pane)
                _t3 = time.time()
                await run_subprocess(
                    ["tmux", "select-pane", "-t", f"{session}:{window_idx}.{pane_idx}"],
                    capture_output=True, timeout=2
                )
                logger.info(f"[TIMING] select-pane took {time.time()-_t3:.3f}s")
                logger.info(f"Switched tmux to pane {target_id}")

                # Verify switch completed (max 1s total, not per-iteration)
                _t4 = time.time()
                _verify_iterations = 0
                _verify_deadline = _t4 + 1.0  # Hard cap at 1 second total
                while time.time() < _verify_deadline:
                    _verify_iterations += 1
                    try:
                        verify_result = await run_subprocess(
                            ["tmux", "display-message", "-t", session, "-p", "#{window_index}:#{pane_index}"],
                            capture_output=True, text=True, timeout=0.5  # Short timeout per call
                        )
                        if verify_result.returncode == 0 and verify_result.stdout.strip() == target_id:
                            switch_verified = True
                            break
                    except subprocess.TimeoutExpired:
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

        # Close existing PTY so next WebSocket connection respawns with new target
        # This ensures the PTY attaches to the newly active pane
        _t5 = time.time()
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
                logger.info("Closed PTY for target switch")
            except Exception as e:
                logger.warning(f"Error closing PTY: {e}")
            app.state.master_fd = None

        # Kill child process
        if app.state.child_pid is not None:
            try:
                os.kill(app.state.child_pid, signal.SIGTERM)
            except Exception:
                pass
            app.state.child_pid = None
        logger.info(f"[TIMING] PTY/child cleanup took {time.time()-_t5:.3f}s")

        # Close active WebSocket to force client reconnect
        _t6 = time.time()
        if app.state.active_websocket is not None:
            try:
                await app.state.active_websocket.close(code=4003, reason="Target switched")
                logger.info("Closed WebSocket for target switch")
            except Exception:
                pass
            app.state.active_websocket = None
        logger.info(f"[TIMING] WebSocket close took {time.time()-_t6:.3f}s")

        # Start background file monitor to detect which log file this target uses
        asyncio.create_task(monitor_log_file_for_target(target_id))

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
            for ws_dir in config.workspace_dirs:
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

    @app.get("/api/transcript")
    async def get_transcript(
        _auth=Depends(verify_token),
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

            # Try systemd first
            try:
                result = await run_subprocess(
                    ["systemctl", "--user", "is-active", "mobile-terminal.service"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    logger.info(f"Restarting via systemd (requested by {client_ip})")
                    subprocess.Popen(
                        ["systemctl", "--user", "restart", "mobile-terminal.service"],
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

    # --- MCP Server Management ---

    @app.get("/api/mcp-servers")
    async def list_mcp_servers(_auth=Depends(verify_token)):
        """List MCP servers from ~/.claude/settings.json."""

        settings, error = load_claude_settings()
        servers = settings.get("mcpServers", {})
        return {
            "servers": servers,
            "source": str(CLAUDE_SETTINGS_FILE),
            "error": error,
        }

    @app.post("/api/mcp-servers")
    async def add_mcp_server(request: Request, _auth=Depends(verify_token)):
        """Add or update an MCP server in ~/.claude/settings.json (upsert)."""

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        name = (body.get("name") or "").strip()
        command = (body.get("command") or "").strip()
        args = body.get("args", [])
        env = body.get("env")

        # Validate name
        if not name or not MCP_NAME_RE.match(name):
            return JSONResponse(
                {"error": f"Invalid name: must match {MCP_NAME_RE.pattern}"},
                status_code=400,
            )

        # Validate command
        if not command:
            return JSONResponse({"error": "Command is required"}, status_code=400)

        # Validate args
        if not isinstance(args, list):
            return JSONResponse({"error": "Args must be an array"}, status_code=400)
        args_size = len(json.dumps(args))
        if args_size > MCP_MAX_ARGS_SIZE:
            return JSONResponse(
                {"error": f"Args too large ({args_size} bytes, max {MCP_MAX_ARGS_SIZE})"},
                status_code=400,
            )

        # Validate env
        if env is not None and not isinstance(env, dict):
            return JSONResponse({"error": "Env must be an object"}, status_code=400)

        # Load settings — refuse to write if file is corrupt
        settings, error = load_claude_settings()
        if error:
            return JSONResponse({"error": error}, status_code=409)

        # Upsert
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}
        updated = name in settings["mcpServers"]
        entry = {"command": command, "args": args}
        if env:
            entry["env"] = env
        settings["mcpServers"][name] = entry

        try:
            save_claude_settings(settings)
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        action = "updated" if updated else "added"
        logger.info(f"MCP server {action}: {name}")
        app.state.audit_log.log(
            "mcp_server_add",
            {"name": name, "command": command, "updated": updated},
        )

        return {"success": True, "name": name, "updated": updated}

    @app.delete("/api/mcp-servers/{name}")
    async def remove_mcp_server(name: str, _auth=Depends(verify_token)):
        """Remove an MCP server from ~/.claude/settings.json."""

        if not MCP_NAME_RE.match(name):
            return JSONResponse({"error": "Invalid server name"}, status_code=400)

        settings, error = load_claude_settings()
        if error:
            return JSONResponse({"error": error}, status_code=409)

        servers = settings.get("mcpServers", {})
        if name not in servers:
            return JSONResponse({"error": f"Server '{name}' not found"}, status_code=404)

        del settings["mcpServers"][name]
        # Clean up empty mcpServers key
        if not settings["mcpServers"]:
            del settings["mcpServers"]

        try:
            save_claude_settings(settings)
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        logger.info(f"MCP server removed: {name}")
        app.state.audit_log.log("mcp_server_remove", {"name": name})

        return {"success": True, "name": name}

    # --- Plugin Management ---

    @app.get("/api/plugins")
    async def list_plugins(_auth=Depends(verify_token)):
        """List enabled plugins and installed plugin IDs."""

        settings, error = load_claude_settings()
        enabled = settings.get("enabledPlugins", {})

        # Read installed plugins for discovery
        installed = []
        try:
            data = json.loads(INSTALLED_PLUGINS_FILE.read_text(encoding="utf-8"))
            installed = list(data.get("plugins", {}).keys())
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            pass

        return {
            "enabled": enabled,
            "installed": installed,
            "error": error,
        }

    @app.post("/api/plugins/toggle")
    async def toggle_plugin(request: Request, _auth=Depends(verify_token)):
        """Enable or disable a plugin in ~/.claude/settings.json."""

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        name = (body.get("name") or "").strip()
        enabled = body.get("enabled", True)

        if not name or not PLUGIN_NAME_RE.match(name):
            return JSONResponse({"error": "Plugin name is required"}, status_code=400)

        settings, error = load_claude_settings()
        if error:
            return JSONResponse({"error": error}, status_code=409)

        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}

        if enabled:
            settings["enabledPlugins"][name] = True
        else:
            settings["enabledPlugins"].pop(name, None)

        try:
            save_claude_settings(settings)
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        action = "enabled" if enabled else "disabled"
        logger.info(f"Plugin {action}: {name}")
        app.state.audit_log.log(
            "plugin_toggle",
            {"name": name, "enabled": enabled},
        )

        return {"success": True, "name": name, "enabled": enabled}

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
        # Get project directory for the target
        repo_path = get_current_repo_path()
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
        1. Check cached mapping
        2. Find Claude process in the target pane
        3. Match Claude process start time to log file creation/first-entry time
        4. Fall back to most recently modified if detection fails
        """
        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return None

        # Check if we have a PINNED mapping for this target (user explicitly selected)
        if target_id:
            cached = app.state.target_log_mapping.get(target_id)
            if cached:
                is_pinned = cached.get("pinned", False) if isinstance(cached, dict) else False
                if is_pinned:
                    cached_path = Path(cached["path"]) if isinstance(cached, dict) else Path(cached)
                    if cached_path.exists():
                        logger.debug(f"Using pinned log file for target {target_id}: {cached_path.name}")
                        return cached_path

        # For non-pinned: ALWAYS use the most recently modified file
        # This is simpler and more reliable than complex process detection
        newest_file = max(jsonl_files, key=lambda f: f.stat().st_mtime)
        logger.info(f"Using newest log file: {newest_file.name} (mtime-based)")
        return newest_file

    # NOTE: Legacy process-based detection code removed in favor of simple mtime approach.
    # The complex detection (matching Claude process to log file via timestamps) was unreliable,
    # especially after plan mode transitions. Simple mtime-based selection is more robust.
    @app.get("/api/log")
    async def get_log(
        request: Request,
        _auth=Depends(verify_token),
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
                        repo_path = get_current_repo_path()
                else:
                    repo_path = get_current_repo_path()
            except Exception:
                repo_path = get_current_repo_path()
        else:
            repo_path = get_current_repo_path()

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
            lines = raw_content.strip().split('\n')

            # Parse JSONL and extract conversation
            conversation = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    msg_type = entry.get('type')
                    message = entry.get('message', {})

                    if msg_type == 'user':
                        content = message.get('content', '')
                        if isinstance(content, str) and content.strip():
                            conversation.append(f"$ {content}")

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
                                        tool_input = block.get('input', {})
                                        # Format tool call nicely
                                        if tool_name == 'Bash':
                                            cmd = tool_input.get('command', '')
                                            conversation.append(f"• Bash: {cmd[:200]}")
                                        elif tool_name in ('Read', 'Edit', 'Write', 'Glob', 'Grep'):
                                            path = tool_input.get('file_path') or tool_input.get('path') or tool_input.get('pattern', '')
                                            conversation.append(f"• {tool_name}: {path[:100]}")
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

            messages = [redact_secrets(msg) for msg in conversation]
            content = '\n\n'.join(messages)

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
    async def clear_log_cache(_auth=Depends(verify_token)):
        """Clear the cached log for the current project."""

        repo_path = get_current_repo_path()
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
    async def list_log_sessions(_auth=Depends(verify_token)):
        """
        List available log files for the current project directory.
        Returns metadata for each session log to allow manual selection.
        """
        from datetime import datetime


        repo_path = get_current_repo_path()
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

    @app.post("/api/log/select")
    async def select_log_session(
        _auth=Depends(verify_token),
        session_id: str = Query(..., description="Session UUID to pin"),
    ):
        """
        Pin a specific log file to the current target.
        This overrides auto-detection until unpinned.
        """

        repo_path = get_current_repo_path()
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
    async def unpin_log_session(_auth=Depends(verify_token)):
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

    @app.get("/api/terminal/capture")
    async def capture_terminal(
        request: Request,
        _auth=Depends(verify_token),
        lines: int = Query(50),
        session: Optional[str] = Query(None),
        pane: int = Query(0),
    ):
        """
        Capture recent terminal output from tmux pane.

        Uses tmux capture-pane to get scrollback buffer.
        Returns last N lines of terminal content.
        Also returns pane_title which Claude Code uses to signal its state.

        Params:
            session: tmux session name (defaults to current_session)
            pane: pane index within session (default 0)
            lines: number of lines to capture (default 50)

        Includes 300ms cache to prevent DoS from rapid polling.
        """
        # Log client ID for debugging duplicate requests
        client_id = request.headers.get('X-Client-ID', 'unknown')[:8]
        logger.debug(f"[{client_id}] GET /api/terminal/capture lines={lines}")


        # Use provided session or fall back to current
        target_session = session or app.state.current_session
        if not target_session:
            return {"content": "", "error": "No session"}

        # Use active_target if no explicit pane provided, otherwise use params
        if app.state.active_target and pane == 0 and session is None:
            # Use active target - convert "window:pane" to "session:window.pane"
            target = get_tmux_target(target_session, app.state.active_target)
            pane_id = app.state.active_target
        else:
            # Use explicit params
            pane_id = str(pane)
            target = f"{target_session}:{0}.{pane}"

        # Check cache first
        cached = get_cached_capture(target_session, pane_id, lines)
        if cached:
            return cached

        try:
            # Capture last N lines from tmux pane
            result = await run_subprocess(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Get pane title - Claude Code sets this to indicate state
            # e.g., "✳ Signal Detection Pending" when waiting for input
            pane_title = ""
            try:
                title_result = await run_subprocess(
                    ["tmux", "display-message", "-p", "-t", target, "#{pane_title}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if title_result.returncode == 0:
                    pane_title = title_result.stdout.strip()
            except Exception:
                pass

            if result.returncode == 0:
                response = {
                    "content": result.stdout,
                    "lines": lines,
                    "pane_title": pane_title,
                    "session": target_session,
                    "pane": pane,
                }
                set_cached_capture(target_session, pane_id, lines, response)
                return response
            else:
                # Target missing or invalid
                if "can't find" in result.stderr.lower() or "no such" in result.stderr.lower():
                    return JSONResponse(
                        {"error": f"Target not found: {target}", "session": target_session, "pane": pane},
                        status_code=409,
                    )
                return {"content": "", "error": result.stderr, "pane_title": pane_title}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Capture timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Failed to capture terminal: {e}")
            return {"content": "", "error": str(e)}

    @app.get("/api/terminal/snapshot")
    async def terminal_snapshot(
        _auth=Depends(verify_token),
        target: Optional[str] = Query(None),
    ):
        """
        Get terminal snapshot for resync after queue overflow.

        Returns tmux capture-pane with ANSI escape sequences (-e) for
        accurate screen reproduction. Limited to 80 lines max.
        Used by client when terminal render queue overflows.
        """

        session_name = app.state.current_session
        if not session_name:
            return {"content": "", "error": "No session"}

        # Use provided target or active target
        if target:
            tmux_target = get_tmux_target(session_name, target)
        elif app.state.active_target:
            tmux_target = get_tmux_target(session_name, app.state.active_target)
        else:
            tmux_target = session_name

        try:
            # Capture with ANSI escape sequences for accurate screen state
            # Limit to 80 lines to keep payload reasonable
            result = await run_subprocess(
                ["tmux", "capture-pane", "-p", "-e", "-S", "-80", "-t", tmux_target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                content = result.stdout or ""
                # If still too large, reduce further
                if len(content) > 50000:  # 50KB max
                    result = await run_subprocess(
                        ["tmux", "capture-pane", "-p", "-e", "-S", "-40", "-t", tmux_target],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    content = result.stdout or "" if result.returncode == 0 else ""
                logger.info(f"Terminal snapshot: {len(content)} chars")
                return {"content": content, "target": tmux_target}
            else:
                logger.warning(f"Snapshot failed: {result.stderr}")
                return {"content": "", "error": result.stderr}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Snapshot timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Snapshot error: {e}")
            return {"content": "", "error": str(e)}

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
        if app.state.active_websocket is not None:
            try:
                await app.state.active_websocket.close(code=4003)  # 4003 = switching repos
            except Exception:
                pass
            app.state.active_websocket = None

        # Cancel read task
        if app.state.read_task is not None:
            app.state.read_task.cancel()
            app.state.read_task = None

        # Kill child process and close pty
        if app.state.child_pid is not None:
            try:
                os.kill(app.state.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # Process already dead
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
            except Exception:
                pass
        app.state.master_fd = None
        app.state.child_pid = None

        # Clear output buffer (don't replay old session's content)
        app.state.output_buffer.clear()

        # Clear target selection and log mappings (pane IDs are session-specific)
        app.state.active_target = None
        app.state.target_log_mapping.clear()

        # Update current session
        app.state.current_session = session
        logger.info(f"Switched to session: {session}")

        return {"status": "ok", "session": session}

    # ========== Command Queue API Endpoints ==========

    @app.post("/api/queue/enqueue")
    async def queue_enqueue(
        text: str = Query(...),
        session: str = Query(...),
        policy: str = Query("auto"),  # "auto" | "safe" | "unsafe"
        id: Optional[str] = Query(None),  # Client-provided ID for idempotency
        pane_id: Optional[str] = Query(None),  # Per-pane scoping
        _auth=Depends(verify_token),
    ):
        """
        Add command to the deferred queue.

        Policy:
        - "auto": server determines safe/unsafe based on text
        - "safe": force auto-send when ready
        - "unsafe": always require manual confirmation

        If `id` is provided and already exists, returns the existing item
        without creating a duplicate (idempotency).
        """

        item, is_new = app.state.command_queue.enqueue(session, text, policy, item_id=id, pane_id=pane_id)

        # Notify connected clients only for new items
        if is_new and app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_update",
                    "action": "add",
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item), "is_new": is_new}

    @app.get("/api/queue/list")
    async def queue_list(
        session: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """List all queued items for a session+pane."""

        items = app.state.command_queue.list_items(session, pane_id)
        paused = app.state.command_queue.is_paused(session, pane_id)
        return {
            "items": [asdict(i) for i in items],
            "paused": paused,
            "session": session,
        }

    @app.post("/api/queue/remove")
    async def queue_remove(
        session: str = Query(...),
        item_id: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Remove an item from the queue."""

        success = app.state.command_queue.dequeue(session, item_id, pane_id)

        if success and app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_update",
                    "action": "remove",
                    "item": {"id": item_id},
                })
            except Exception:
                pass

        return {"status": "ok" if success else "not_found"}

    @app.post("/api/queue/reorder")
    async def queue_reorder(
        session: str = Query(...),
        item_id: str = Query(...),
        new_index: int = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Reorder an item in the queue."""

        success = app.state.command_queue.reorder(session, item_id, new_index, pane_id)
        return {"status": "ok" if success else "not_found"}

    @app.post("/api/queue/pause")
    async def queue_pause(
        session: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Pause queue processing for a session+pane."""

        app.state.command_queue.pause(session, pane_id)

        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_state",
                    "paused": True,
                    "count": len(app.state.command_queue.list_items(session, pane_id)),
                })
            except Exception:
                pass

        return {"status": "ok", "paused": True}

    @app.post("/api/queue/resume")
    async def queue_resume(
        session: str = Query(...),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Resume queue processing for a session+pane."""

        app.state.command_queue.resume(session, pane_id)

        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_state",
                    "paused": False,
                    "count": len(app.state.command_queue.list_items(session, pane_id)),
                })
            except Exception:
                pass

        return {"status": "ok", "paused": False}

    @app.post("/api/queue/flush")
    async def queue_flush(
        session: str = Query(...),
        confirm: bool = Query(False),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Clear all queued items. Requires confirm=true."""

        if not confirm:
            items = app.state.command_queue.list_items(session, pane_id)
            return {"status": "confirm_required", "count": len(items)}

        count = app.state.command_queue.flush(session, pane_id)

        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_state",
                    "paused": False,
                    "count": 0,
                })
            except Exception:
                pass

        return {"status": "ok", "cleared": count}

    @app.post("/api/queue/send-next")
    async def queue_send_next(
        session: str = Query(...),
        item_id: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """
        Manually send the next unsafe item (or specific item).
        Bypasses policy check for one item.
        """

        item = await app.state.command_queue.send_next_unsafe(session, item_id, pane_id)

        if item:
            return {"status": "ok", "item": asdict(item)}
        else:
            return {"status": "not_found", "message": "No unsafe items in queue"}

    # ========== End Command Queue API ==========

    # ========== Preview/Snapshot API ==========

    @app.post("/api/rollback/preview/capture")
    async def capture_preview(
        label: str = Query("manual"),
        _auth=Depends(verify_token),
    ):
        """Capture a snapshot of current session state."""

        session = app.state.current_session
        if not session:
            return JSONResponse({"error": "No active session"}, status_code=400)

        # Get log content (reuse /api/log logic)
        try:
            repo_path = get_current_repo_path()
            if repo_path:
                project_id = get_project_id(repo_path, strip_leading=True)
                claude_dir = Path.home() / ".claude" / "projects" / project_id
                jsonl_files = sorted(claude_dir.glob("*.jsonl"),
                                   key=lambda f: f.stat().st_mtime, reverse=True)
                log_content = jsonl_files[0].read_text(errors="replace") if jsonl_files else ""
            else:
                log_content = ""
        except Exception as e:
            log_content = f"[Error reading log: {e}]"

        # Capture terminal (last 50 lines)
        try:
            # Use active target pane if set, otherwise fall back to session default
            target = get_tmux_target(session, app.state.active_target)
            result = await run_subprocess(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", "-50"],
                capture_output=True, text=True, timeout=5
            )
            terminal_text = result.stdout if result.returncode == 0 else ""
        except Exception:
            terminal_text = ""

        # Get queue state
        queue_state = [asdict(item) for item in app.state.command_queue.list_items(session)]

        # Capture snapshot
        logger.info(f"Capturing snapshot: session={session}, label={label}, log_len={len(log_content)}, term_len={len(terminal_text)}")
        snapshot = app.state.snapshot_buffer.capture(
            session, label, log_content, terminal_text, queue_state
        )

        if snapshot:
            logger.info(f"Snapshot created: {snapshot['id']}")
            app.state.audit_log.log("snapshot_capture", {"snap_id": snapshot["id"], "label": label})
            return {"success": True, "snapshot": {"id": snapshot["id"], "label": label}}
        logger.info("Snapshot skipped: content unchanged")
        return {"success": True, "snapshot": None, "reason": "unchanged"}

    @app.get("/api/rollback/previews")
    async def list_previews(
        limit: int = Query(50),
        pane_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """List available snapshots, optionally filtered by pane_id."""

        session = app.state.current_session
        target = pane_id or app.state.active_target
        buf = app.state.snapshot_buffer

        # Try target-scoped key first, then fall back to session-only
        snap_key = f"{session}:{target}" if target else session
        snapshots = buf.list_snapshots(snap_key, limit)
        if not snapshots and target:
            # Fall back to session-only snapshots (legacy)
            snapshots = buf.list_snapshots(session, limit)

        logger.info(f"List snapshots: key={snap_key}, count={len(snapshots)}")
        return {"session": session, "pane_id": target, "snapshots": snapshots}

    @app.get("/api/rollback/preview/{snap_id}")
    async def get_preview(
        snap_id: str,
        _auth=Depends(verify_token),
    ):
        """Get full snapshot data. Populates heavy fields on demand."""

        session = app.state.current_session
        target = app.state.active_target
        buf = app.state.snapshot_buffer

        # Search in target-scoped key first, then session
        snap_key = f"{session}:{target}" if target else session
        snapshot = buf.get_snapshot(snap_key, snap_id)
        if not snapshot and target:
            snapshot = buf.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        # Populate heavy fields on demand if they're empty (lazy loading)
        if not snapshot.get("terminal_text") and snapshot.get("pane_id"):
            try:
                tmux_t = get_tmux_target(session, snapshot["pane_id"])
                cap = await run_subprocess(
                    ["tmux", "capture-pane", "-p", "-S", "-100", "-t", tmux_t],
                    capture_output=True, text=True, timeout=3,
                )
                if cap.returncode == 0:
                    snapshot["terminal_text"] = cap.stdout or ""
            except Exception:
                pass

        if not snapshot.get("log_entries") and snapshot.get("log_path") and snapshot.get("log_offset"):
            try:
                lp = Path(snapshot["log_path"])
                if lp.exists():
                    # Read last 4KB before the offset for context
                    offset = snapshot["log_offset"]
                    read_start = max(0, offset - 4096)
                    with open(lp, 'rb') as f:
                        f.seek(read_start)
                        data = f.read(offset - read_start)
                    snapshot["log_entries"] = data.decode('utf-8', errors='replace')
            except Exception:
                pass

        return snapshot

    @app.post("/api/rollback/preview/{snap_id}/annotate")
    async def annotate_snapshot(
        snap_id: str,
        request: Request,
        _auth=Depends(verify_token),
    ):
        """Add a note or image_path to a snapshot."""

        body = await request.json()
        note = body.get("note", "")
        image_path = body.get("image_path")

        # Cap note at 500 chars
        if note and len(note) > 500:
            note = note[:500]

        session = app.state.current_session
        target = app.state.active_target
        buf = app.state.snapshot_buffer

        # Search in target-scoped key first, then session
        snap_key = f"{session}:{target}" if target else session
        snapshot = None
        with buf._lock:
            snaps = buf._snapshots.get(snap_key, {})
            if snap_id in snaps:
                snapshot = snaps[snap_id]
            elif target:
                snaps = buf._snapshots.get(session, {})
                if snap_id in snaps:
                    snapshot = snaps[snap_id]

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        if note is not None:
            snapshot["note"] = note
        if image_path is not None:
            snapshot["image_path"] = image_path

        app.state.audit_log.log("snapshot_annotate", {"snap_id": snap_id, "note": note[:50] if note else ""})
        return {"success": True, "snap_id": snap_id}

    @app.post("/api/rollback/preview/select")
    async def select_preview(
        snap_id: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Enter or exit preview mode (snap_id=null exits)."""

        session = app.state.current_session

        if snap_id:
            # Verify snapshot exists
            snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)
            if not snapshot:
                return JSONResponse({"error": "Snapshot not found"}, status_code=404)
            app.state.audit_log.log("preview_enter", {"snap_id": snap_id})
        else:
            app.state.audit_log.log("preview_exit", {})

        return {"success": True, "preview_mode": snap_id is not None, "snap_id": snap_id}

    @app.post("/api/rollback/preview/{snap_id}/pin")
    async def pin_snapshot(
        snap_id: str,
        pinned: bool = Query(True),
        _auth=Depends(verify_token),
    ):
        """Pin or unpin a snapshot to prevent eviction."""

        session = app.state.current_session
        success = app.state.snapshot_buffer.pin_snapshot(session, snap_id, pinned)

        if not success:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        app.state.audit_log.log("snapshot_pin", {"snap_id": snap_id, "pinned": pinned})
        return {"success": True, "snap_id": snap_id, "pinned": pinned}

    @app.get("/api/rollback/preview/{snap_id}/export")
    async def export_snapshot(
        snap_id: str,
        _auth=Depends(verify_token),
    ):
        """Export snapshot as JSON file."""

        session = app.state.current_session
        snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        app.state.audit_log.log("snapshot_export", {"snap_id": snap_id})

        from starlette.responses import Response
        return Response(
            json.dumps(snapshot, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={snap_id}.json"}
        )

    @app.get("/api/rollback/preview/diff")
    async def diff_snapshots(
        snap_a: str = Query(...),
        snap_b: str = Query(...),
        _auth=Depends(verify_token),
    ):
        """Compare two snapshots."""

        session = app.state.current_session
        a = app.state.snapshot_buffer.get_snapshot(session, snap_a)
        b = app.state.snapshot_buffer.get_snapshot(session, snap_b)

        if not a:
            return JSONResponse({"error": f"Snapshot {snap_a} not found"}, status_code=404)
        if not b:
            return JSONResponse({"error": f"Snapshot {snap_b} not found"}, status_code=404)

        return {
            "a": {"id": snap_a, "timestamp": a["timestamp"], "label": a["label"]},
            "b": {"id": snap_b, "timestamp": b["timestamp"], "label": b["label"]},
            "log_changed": a["log_hash"] != b["log_hash"],
            "terminal_changed": a["terminal_text"] != b["terminal_text"],
            "queue_changed": a["queue_state"] != b["queue_state"],
        }

    @app.post("/api/rollback/preview/clear")
    async def clear_previews(
        _auth=Depends(verify_token),
    ):
        """Clear all snapshots for current session."""

        session = app.state.current_session
        count = app.state.snapshot_buffer.clear(session)
        app.state.audit_log.log("snapshots_cleared", {"count": count})
        logger.info(f"Cleared {count} snapshots for session {session}")
        return {"success": True, "cleared": count}

    @app.get("/api/rollback/audit")
    async def get_audit_log(
        limit: int = Query(100),
        _auth=Depends(verify_token),
    ):
        """Get recent audit log entries."""

        entries = app.state.audit_log.get_entries(limit)
        return {"entries": entries}

    @app.post("/api/rollback/audit/log")
    async def log_audit_action(
        action: str = Query(...),
        details: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Log an audit action from client."""

        detail_dict = {}
        if details:
            try:
                detail_dict = json.loads(details)
            except:
                detail_dict = {"raw": details}

        app.state.audit_log.log(action, detail_dict)
        return {"success": True}

    # ========== End Preview/Snapshot API ==========

    # ========== Process Management API ==========

    @app.post("/api/process/terminate")
    async def terminate_process(
        _auth=Depends(verify_token),
        force: bool = Query(False),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Terminate the PTY process.

        First tries SIGTERM, then SIGKILL if force=True or if SIGTERM fails.
        """

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        child_pid = app.state.child_pid
        if not child_pid:
            return JSONResponse({"error": "No process running"}, status_code=400)

        try:
            # First try SIGTERM (graceful)
            os.kill(child_pid, signal.SIGTERM)
            app.state.audit_log.log("process_terminate", {"pid": child_pid, "signal": "SIGTERM"})

            # Wait briefly for process to terminate
            await asyncio.sleep(0.5)

            # Check if still running
            try:
                os.kill(child_pid, 0)  # Signal 0 just checks if process exists
                still_running = True
            except OSError:
                still_running = False

            if still_running and force:
                # SIGKILL as fallback
                os.kill(child_pid, signal.SIGKILL)
                app.state.audit_log.log("process_terminate", {"pid": child_pid, "signal": "SIGKILL"})
                await asyncio.sleep(0.2)

            # Clean up PTY state
            if app.state.master_fd is not None:
                try:
                    os.close(app.state.master_fd)
                except Exception:
                    pass
            app.state.master_fd = None
            app.state.child_pid = None

            return {
                "success": True,
                "pid": child_pid,
                "method": "SIGKILL" if (still_running and force) else "SIGTERM"
            }

        except ProcessLookupError:
            # Process already dead
            app.state.master_fd = None
            app.state.child_pid = None
            return {"success": True, "pid": child_pid, "method": "already_dead"}
        except Exception as e:
            logger.error(f"Failed to terminate process: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/process/respawn")
    async def respawn_process(
        _auth=Depends(verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Respawn the PTY process.

        Terminates existing process (if any) and creates a new one.
        """

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        old_pid = app.state.child_pid

        # Terminate existing process if any
        if old_pid:
            try:
                os.kill(old_pid, signal.SIGTERM)
                await asyncio.sleep(0.3)
                try:
                    os.kill(old_pid, 0)
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning(f"Error terminating old process: {e}")

        # Clean up old PTY
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
            except Exception:
                pass
        app.state.master_fd = None
        app.state.child_pid = None
        app.state.output_buffer.clear()

        # Spawn new PTY
        try:
            session_name = app.state.current_session
            master_fd, child_pid = spawn_tmux(session_name)
            app.state.master_fd = master_fd
            app.state.child_pid = child_pid

            app.state.audit_log.log("process_respawn", {
                "old_pid": old_pid,
                "new_pid": child_pid,
                "session": session_name
            })

            return {
                "success": True,
                "old_pid": old_pid,
                "new_pid": child_pid,
                "session": session_name
            }
        except Exception as e:
            logger.error(f"Failed to respawn process: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/process/status")
    async def process_status(
        _auth=Depends(verify_token),
    ):
        """Get current process status."""

        child_pid = app.state.child_pid
        is_running = False

        if child_pid:
            try:
                os.kill(child_pid, 0)
                is_running = True
            except OSError:
                is_running = False

        return {
            "pid": child_pid,
            "is_running": is_running,
            "has_pty": app.state.master_fd is not None,
            "session": app.state.current_session
        }

    # ========== End Process Management API ==========


    def _build_observe_context(pane_id: str) -> Optional[ObserveContext]:
        """Build ObserveContext from a pane_id with one tmux call."""
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

    @app.get("/api/health/agent")
    @app.get("/api/health/claude")  # permanent alias
    async def check_agent_health(
        pane_id: str = Query(...),
        _auth=Depends(verify_token),
    ):
        """
        Check agent status for a specific pane.

        Returns flat Observation JSON: agent_type, agent_name, running, pid,
        phase, detail, tool, active, waiting_reason, permission_tool, permission_target.
        """

        ctx = _build_observe_context(pane_id)
        if ctx is None:
            return Observation(
                agent_type=app.state.driver.id(),
                agent_name=app.state.driver.display_name(),
            ).to_dict()

        obs = app.state.driver.observe(ctx)
        return obs.to_dict()

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

    @app.get("/api/team/state")
    async def get_team_state(
        session: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Batch endpoint: phase + permission + git info for all team panes."""

        sess = session or app.state.current_session

        # Get all panes (include pane_pid and pane_title for driver observe)
        result = await run_subprocess(
            ["tmux", "list-panes", "-s", "-t", sess,
             "-F", "#{window_index}:#{pane_index}|#{pane_current_path}|#{window_name}|#{pane_pid}|#{pane_title}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return {"has_team": False, "session": sess, "team": None}

        leader = None
        agents = []
        driver = app.state.driver
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) < 3:
                continue
            target_id = parts[0]
            cwd = parts[1]
            window_name = parts[2]
            pane_pid_str = parts[3] if len(parts) > 3 else ""
            pane_title = parts[4] if len(parts) > 4 else ""

            if window_name == "leader":
                role = "leader"
                name = "leader"
            elif window_name.startswith("a-"):
                role = "agent"
                name = window_name
            else:
                continue  # Skip non-team panes

            cwd_path = Path(cwd)
            ctx = ObserveContext(
                session_name=sess,
                target=target_id,
                tmux_target=get_tmux_target(sess, target_id),
                shell_pid=int(pane_pid_str) if pane_pid_str.isdigit() else None,
                pane_title=pane_title,
                repo_path=cwd_path if cwd_path.exists() else None,
            )
            obs = driver.observe(ctx)
            git_info = _get_git_info_cached(cwd_path)

            entry = {
                "target_id": target_id,
                "agent_name": name,
                "team_role": role,
                "phase": obs.phase,
                "detail": obs.detail,
                "tool": obs.tool,
                "active": obs.active,
                "waiting_reason": obs.waiting_reason,
                "permission": {
                    "tool": obs.permission_tool or "",
                    "target": obs.permission_target or "",
                } if obs.waiting_reason == "permission" else None,
                "git": git_info,
            }

            if role == "leader":
                leader = entry
            else:
                agents.append(entry)

        has_team = leader is not None or len(agents) > 0

        return {
            "has_team": has_team,
            "session": sess,
            "team": {
                "leader": leader,
                "agents": sorted(agents, key=lambda a: a["agent_name"]),
            } if has_team else None,
        }

    @app.get("/api/team/capture")
    async def get_team_capture(
        session: Optional[str] = Query(None),
        lines: int = Query(8),
        _auth=Depends(verify_token),
    ):
        """Batch capture last N lines from each team pane."""

        sess = session or app.state.current_session
        lines = max(1, min(lines, 50))  # Clamp 1-50

        # Discover team panes (same filter as get_team_state)
        result = await run_subprocess(
            ["tmux", "list-panes", "-s", "-t", sess,
             "-F", "#{window_index}:#{pane_index}|#{window_name}|#{pane_title}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return {"captures": {}}

        captures = {}
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) < 3:
                continue
            target_id = parts[0]
            window_name = parts[1]
            pane_title = parts[2]

            if window_name != "leader" and not window_name.startswith("a-"):
                continue

            # Check cache first
            cached = get_cached_capture(sess, target_id, lines)
            if cached is not None:
                captures[target_id] = cached
                continue

            # Capture pane content
            tmux_target = get_tmux_target(sess, target_id)
            try:
                cap = await run_subprocess(
                    ["tmux", "capture-pane", "-t", tmux_target, "-p", f"-S", f"-{lines}"],
                    capture_output=True, text=True, timeout=5
                )
                content = cap.stdout if cap.returncode == 0 else ""
            except Exception:
                content = ""

            entry = {"content": content, "pane_title": pane_title}
            set_cached_capture(sess, target_id, lines, entry)
            captures[target_id] = entry

        return {"captures": captures}

    @app.post("/api/team/send")
    async def team_send(
        target_id: str = Query(...),
        text: str = Query(...),
        session: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Send input to a specific team pane without switching activeTarget."""

        sess = session or app.state.current_session

        # Validate target_id is a team pane
        result = await run_subprocess(
            ["tmux", "list-panes", "-s", "-t", sess,
             "-F", "#{window_index}:#{pane_index}|#{window_name}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return JSONResponse({"error": "Cannot list panes"}, status_code=500)

        is_team_pane = False
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) >= 2 and parts[0] == target_id:
                wname = parts[1]
                if wname == "leader" or wname.startswith("a-"):
                    is_team_pane = True
                break

        if not is_team_pane:
            return JSONResponse({"error": "Not a team pane", "target_id": target_id}, status_code=400)

        tmux_target = get_tmux_target(sess, target_id)

        try:
            # Send text literally, then Enter
            await run_subprocess(
                ["tmux", "send-keys", "-t", tmux_target, "-l", text],
                capture_output=True, text=True, timeout=5
            )
            await run_subprocess(
                ["tmux", "send-keys", "-t", tmux_target, "Enter"],
                capture_output=True, text=True, timeout=5
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        return {"success": True, "target_id": target_id}

    @app.post("/api/team/dispatch")
    async def team_dispatch(
        plan_filename: str = Query(...),
        include_context: bool = Query(True),
        preferences: Optional[str] = Query(None),
        dispatch_id: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        _auth=Depends(verify_token),
    ):
        """Assemble dispatch.md from plan + context + roster, write to leader CWD, send instruction."""

        sess = session or app.state.current_session

        # Sanitize plan_filename
        if not re.match(r'^[\w\-\.]+\.md$', plan_filename):
            return JSONResponse({"error": "Invalid plan filename"}, status_code=400)

        # Sanitize preferences
        if preferences:
            preferences = re.sub(r'[\x00-\x1f\x7f]', '', preferences)[:500]

        # Read plan file
        plan_path = Path.home() / ".claude" / "plans" / plan_filename
        if not plan_path.is_file():
            return JSONResponse({"error": f"Plan not found: {plan_filename}"}, status_code=404)
        try:
            plan_content = plan_path.read_text(encoding="utf-8")
        except Exception as e:
            return JSONResponse({"error": f"Cannot read plan: {e}"}, status_code=500)

        # Discover team panes
        result = await run_subprocess(
            ["tmux", "list-panes", "-s", "-t", sess,
             "-F", "#{window_index}:#{pane_index}|#{pane_current_path}|#{window_name}|#{pane_pid}|#{pane_title}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return JSONResponse({"error": "Cannot list panes"}, status_code=500)

        leader = None
        agents = []
        driver = app.state.driver
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) < 3:
                continue
            target_id = parts[0]
            cwd = parts[1]
            window_name = parts[2]
            pane_pid_str = parts[3] if len(parts) > 3 else ""
            pane_title = parts[4] if len(parts) > 4 else ""

            if window_name == "leader":
                role = "leader"
                name = "leader"
            elif window_name.startswith("a-"):
                role = "agent"
                name = window_name
            else:
                continue

            cwd_path = Path(cwd)
            git_info = _get_git_info_cached(cwd_path)
            ctx = ObserveContext(
                session_name=sess,
                target=target_id,
                tmux_target=get_tmux_target(sess, target_id),
                shell_pid=int(pane_pid_str) if pane_pid_str.isdigit() else None,
                pane_title=pane_title,
                repo_path=cwd_path if cwd_path.exists() else None,
            )
            obs = driver.observe(ctx)

            entry = {
                "target_id": target_id,
                "agent_name": name,
                "team_role": role,
                "cwd": cwd,
                "phase": obs.phase,
                "git": git_info,
            }

            if role == "leader":
                leader = entry
            else:
                agents.append(entry)

        if leader is None:
            return JSONResponse({"error": "No leader pane found"}, status_code=404)

        leader_cwd = Path(leader["cwd"])

        # Generate dispatch_id if not provided
        from datetime import datetime
        if not dispatch_id:
            dispatch_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

        # Get git root (best-effort)
        git_root = ""
        try:
            gr = await run_subprocess(
                ["git", "-C", str(leader_cwd), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=2
            )
            if gr.returncode == 0:
                git_root = gr.stdout.strip()
        except Exception:
            pass

        # Optionally read CONTEXT.md
        context_content = ""
        if include_context:
            context_path = leader_cwd / ".claude" / "CONTEXT.md"
            try:
                if context_path.is_file():
                    context_content = context_path.read_text(encoding="utf-8")[:3000]
            except Exception:
                pass

        # Build roster table
        roster_lines = ["| Agent | Branch | CWD | Phase |", "|-------|--------|-----|-------|"]
        all_agents = sorted(agents, key=lambda a: a["agent_name"])
        warning_main_agents = []
        for a in all_agents:
            branch = a["git"].get("branch") or "unknown"
            if a["git"].get("is_main"):
                warning_main_agents.append(a["agent_name"])
            roster_lines.append(f"| {a['agent_name']} | {branch} | {a['cwd']} | {a['phase']} |")

        roster_table = "\n".join(roster_lines)

        # Assemble dispatch markdown
        now_iso = datetime.now().isoformat()
        dispatch_md = f"""# Team Dispatch

**ID:** {dispatch_id}
**Plan:** {plan_filename}
**Dispatched:** {now_iso}
**Progress file:** `.claude/team-memory.md`

## What to do now

1. Read and understand the plan below
2. Propose a task split (agent -> task) in your response
3. Flag any shared-file conflict risks
4. Assign tasks to agents and begin execution
5. Track progress in `.claude/team-memory.md`
6. Ask me (human) only when blocked

## Response contract

Reply with:
- Task split (agent -> task)
- Any shared files / conflict risks
- First actions you are taking now
- Questions only if blocked

## Team Roster

{roster_table}

**Leader CWD:** {leader_cwd}
**Git root:** {git_root}

## Plan

{plan_content}
"""

        if include_context and context_content:
            dispatch_md += f"""
## Background

{context_content}
"""

        dispatch_md += """
## Constraints

- Each agent works in its own git worktree on its own branch
- Nobody commits to main/master
- You (leader) are the single coordination point
- When agents finish, review their branches before merging
"""

        if preferences:
            dispatch_md += f"""
## Additional Instructions

{preferences}
"""

        # Write dispatch files
        dispatch_dir = leader_cwd / ".claude"
        dispatch_archive_dir = dispatch_dir / "dispatch"
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        dispatch_archive_dir.mkdir(parents=True, exist_ok=True)

        dispatch_file = dispatch_dir / "dispatch.md"
        archive_file = dispatch_archive_dir / f"dispatch-{dispatch_id}.md"

        try:
            dispatch_file.write_text(dispatch_md, encoding="utf-8")
            archive_file.write_text(dispatch_md, encoding="utf-8")
        except Exception as e:
            return JSONResponse({"error": f"Failed to write dispatch file: {e}"}, status_code=500)

        # Send instruction to leader pane
        tmux_target = get_tmux_target(sess, leader["target_id"])
        instruction = f"Read .claude/dispatch.md and execute the plan. Dispatch ID: {dispatch_id}"
        try:
            await run_subprocess(
                ["tmux", "send-keys", "-t", tmux_target, "-l", instruction],
                capture_output=True, text=True, timeout=5
            )
            await run_subprocess(
                ["tmux", "send-keys", "-t", tmux_target, "Enter"],
                capture_output=True, text=True, timeout=5
            )
        except Exception as e:
            return JSONResponse({"error": f"Failed to send to leader: {e}"}, status_code=500)

        app.state.audit_log.log("team_dispatch", {
            "dispatch_id": dispatch_id,
            "plan": plan_filename,
            "leader_target": leader["target_id"],
            "agents": [a["agent_name"] for a in all_agents],
        })

        return {
            "success": True,
            "dispatch_id": dispatch_id,
            "dispatch_path": str(dispatch_file),
            "leader_target": leader["target_id"],
            "agents_count": len(all_agents),
            "agents": [a["agent_name"] for a in all_agents],
            "warning_main_agents": warning_main_agents,
        }

    @app.get("/api/status/phase")
    async def get_status_phase(
        pane_id: str = Query(None),
        _auth=Depends(verify_token),
    ):
        """Get agent's current phase for the status strip. Delegates to driver."""

        target = pane_id or app.state.active_target
        ctx = _build_observe_context(target) if target else None

        if ctx is None:
            return {
                "phase": "idle", "detail": "", "tool": "",
                "session": app.state.current_session,
                "pane_id": target or "",
                "claude_running": False,
            }

        obs = app.state.driver.observe(ctx)
        return {
            "phase": obs.phase,
            "detail": obs.detail,
            "tool": obs.tool,
            "session": app.state.current_session,
            "pane_id": target or "",
            "claude_running": obs.running,
        }

    @app.post("/api/agent/start")
    @app.post("/api/claude/start")  # permanent alias
    async def start_agent_in_pane(
        request: Request,
        pane_id: str = Query(...),
        _auth=Depends(verify_token),
    ):
        """
        Start agent in a pane if not already running.

        Returns 409 if agent is already running.
        Uses driver.start_command() for the default, or repo's startup_command.
        """

        driver = app.state.driver
        agent_name = driver.display_name()

        # Check if agent is already running via driver.observe()
        ctx = _build_observe_context(pane_id)
        if ctx is None:
            return JSONResponse({"error": "Pane not found"}, status_code=404)

        obs = driver.observe(ctx)
        if obs.running:
            return JSONResponse({
                "error": f"{agent_name} is already running in this pane",
                "agent_pid": obs.pid,
            }, status_code=409)

        # Get startup command from request body or find matching repo
        default_cmd = driver.start_command()[0]
        startup_cmd = default_cmd
        repo_label = None

        try:
            body = await request.json()
            if body.get("startup_command"):
                startup_cmd = body["startup_command"]
            repo_label = body.get("repo_label")
        except Exception:
            pass  # No body or invalid JSON, use default

        # If repo_label provided, look up its startup_command
        if repo_label:
            repo = next((r for r in config.repos if r.label == repo_label), None)
            if repo and repo.startup_command:
                startup_cmd = repo.startup_command

        # Validate startup command
        if "\n" in startup_cmd or "\r" in startup_cmd:
            return JSONResponse({"error": "startup_command cannot contain newlines"}, status_code=400)
        if len(startup_cmd) > 200:
            return JSONResponse({"error": "startup_command exceeds 200 character limit"}, status_code=400)

        try:
            # Clear CLAUDECODE to prevent nested-session errors, then run command
            actual_cmd = f"unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; {startup_cmd}"
            await run_subprocess(
                ["tmux", "send-keys", "-t", pane_id, "-l", actual_cmd],
                capture_output=True, timeout=5
            )
            await run_subprocess(
                ["tmux", "send-keys", "-t", pane_id, "Enter"],
                capture_output=True, timeout=5
            )

            logger.info(f"Started '{startup_cmd}' in pane {pane_id}")
            app.state.audit_log.log("agent_start", {
                "pane_id": pane_id,
                "command": startup_cmd,
                "repo_label": repo_label,
                "agent_type": driver.id(),
            })

            return {
                "success": True,
                "pane_id": pane_id,
                "command": startup_cmd,
            }

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": f"Timeout starting {agent_name}"}, status_code=504)
        except Exception as e:
            logger.error(f"Error starting {agent_name}: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ========== Git Rollback API ==========

    _pr_info_cache: Dict[str, dict] = {}  # repo_path -> {data, time}
    PR_INFO_CACHE_TTL = 120.0  # seconds

    @app.get("/api/rollback/git/status")
    async def git_status(
        _auth=Depends(verify_token),
    ):
        """Get current git status: branch, dirty, ahead/behind, lock status."""

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """List recent git commits."""

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """Get unified history: commits + snapshots merged chronologically."""

        items = []
        session = app.state.current_session

        # Get commits with unix timestamps
        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """Get commit details including changed files."""

        # Validate hash format (security)
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """Preview revert without executing."""

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Execute git revert."""

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
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

        repo_path = get_current_repo_path()
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
            head_before = await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()

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
            new_head = await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()

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
        _auth=Depends(verify_token),
    ):
        """Undo a revert by reverting the revert commit (non-destructive)."""

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', revert_commit):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
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
            new_head = await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()

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
        _auth=Depends(verify_token),
    ):
        """Create a stash with auto-generated message."""

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """List all stashes."""

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """Apply a stash without removing it."""

        # Validate stash ref format
        if not re.match(r'^stash@\{\d+\}$', ref):
            return JSONResponse({"error": "Invalid stash ref format"}, status_code=400)

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
    ):
        """Drop a stash."""

        # Validate stash ref format
        if not re.match(r'^stash@\{\d+\}$', ref):
            return JSONResponse({"error": "Invalid stash ref format"}, status_code=400)

        repo_path = get_current_repo_path()
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
        _auth=Depends(verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Discard all uncommitted changes. Optionally remove untracked files."""

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        repo_path = get_current_repo_path()
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

    # ========== End Git Rollback API ==========

    @app.post("/api/send")
    async def send_line(
        text: str = Query(...),
        session: str = Query(...),
        msg_id: str = Query(...),
        _auth=Depends(verify_token),
    ):
        """
        Send a line of text to the terminal with Enter.
        Uses the InputQueue for serialized, atomic writes with ACKs.
        """

        # Validate session matches current
        if session != app.state.current_session:
            return JSONResponse({
                "error": "Session mismatch",
                "expected": app.state.current_session,
                "got": session
            }, status_code=400)

        if app.state.master_fd is None:
            return JSONResponse({"error": "No active terminal"}, status_code=400)

        # Atomic write: text + carriage return
        data = (text + "\r").encode("utf-8")

        # Queue the send (will wait for quiet period and send ACK)
        success = await app.state.input_queue.send(
            msg_id,
            data,
            app.state.master_fd,
            app.state.active_websocket
        )

        if success:
            return {"status": "ok", "id": msg_id}
        else:
            return JSONResponse({"error": "Send timeout", "id": msg_id}, status_code=504)

    @app.post("/api/sendkey")
    async def send_key(
        key: str = Query(...),
        session: str = Query(...),
        msg_id: str = Query(...),
        _auth=Depends(verify_token),
    ):
        """
        Send a control key using tmux send-keys.
        Supports: C-c, C-d, C-z, C-l, Tab, Escape, Enter, Up, Down, Left, Right, etc.
        """

        # Validate session matches current
        if session != app.state.current_session:
            return JSONResponse({
                "error": "Session mismatch",
                "expected": app.state.current_session,
                "got": session
            }, status_code=400)

        # Map common key names to tmux send-keys format
        key_map = {
            "ctrl-c": "C-c",
            "ctrl-d": "C-d",
            "ctrl-z": "C-z",
            "ctrl-l": "C-l",
            "ctrl-a": "C-a",
            "ctrl-e": "C-e",
            "ctrl-w": "C-w",
            "ctrl-u": "C-u",
            "ctrl-k": "C-k",
            "ctrl-r": "C-r",
            "ctrl-o": "C-o",
            "ctrl-b": "C-b",
            "tab": "Tab",
            "escape": "Escape",
            "esc": "Escape",
            "enter": "Enter",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "pageup": "PageUp",
            "pagedown": "PageDown",
            "home": "Home",
            "end": "End",
        }

        tmux_key = key_map.get(key.lower(), key)
        # Use active target pane if set, otherwise default to 0.0
        target = get_tmux_target(session, app.state.active_target)

        try:
            result = await run_subprocess(
                ["tmux", "send-keys", "-t", target, tmux_key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return JSONResponse({
                    "error": f"tmux send-keys failed: {result.stderr}",
                    "id": msg_id
                }, status_code=500)

            # Send ACK via WebSocket if connected
            if app.state.active_websocket:
                try:
                    await app.state.active_websocket.send_json({
                        "type": "ack",
                        "id": msg_id
                    })
                except Exception:
                    pass

            return {"status": "ok", "id": msg_id, "key": tmux_key}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "tmux command timeout", "id": msg_id}, status_code=504)
        except Exception as e:
            return JSONResponse({"error": str(e), "id": msg_id}, status_code=500)

    @app.websocket("/ws/terminal")
    async def terminal_websocket(websocket: WebSocket, _auth=Depends(verify_token)):
        """WebSocket endpoint for terminal I/O."""
        _ws_start = time.time()
        logger.info(f"[TIMING] WebSocket /ws/terminal START")

        # Use lock to prevent concurrent connection setup
        async with app.state.ws_connect_lock:
            # Rate limit connections - minimum 500ms between accepts
            now = time.time()
            elapsed = now - app.state.last_ws_connect
            if elapsed < 0.5:
                logger.info(f"Rate limiting WebSocket connection ({elapsed:.2f}s since last)")
                await websocket.close(code=4004)  # 4004 = rate limited
                return
            app.state.last_ws_connect = now

            await websocket.accept()
            logger.info("WebSocket connection accepted")

            # Close any existing connection (single client mode)
            if app.state.active_websocket is not None:
                try:
                    await app.state.active_websocket.close(code=4002)
                    logger.info("Closed previous WebSocket connection")
                except Exception:
                    pass
            if app.state.read_task is not None:
                app.state.read_task.cancel()
                app.state.read_task = None

            app.state.active_websocket = websocket
        logger.info(f"[TIMING] WebSocket lock+accept took {time.time()-_ws_start:.3f}s")

        # Spawn tmux if not already running
        _spawn_start = time.time()
        if app.state.master_fd is None:
            try:
                session_name = app.state.current_session
                master_fd, child_pid = spawn_tmux(session_name)
                app.state.master_fd = master_fd
                app.state.child_pid = child_pid
                logger.info(f"Spawned tmux session: {session_name}")
            except Exception as e:
                logger.error(f"Failed to spawn tmux: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})
                await websocket.close()
                return
        logger.info(f"[TIMING] spawn_tmux took {time.time()-_spawn_start:.3f}s")
        master_fd = app.state.master_fd
        output_buffer = app.state.output_buffer

        # Send hello handshake FIRST - client expects this within 2s
        # Must be sent before capture-pane which can be slow
        _hello_start = time.time()
        try:
            hello_msg = {
                "type": "hello",
                "session": app.state.current_session,
                "pid": app.state.child_pid,
                "started_at": int(time.time()),
            }
            await websocket.send_json(hello_msg)
            logger.info(f"Sent hello handshake: {hello_msg}")
        except Exception as e:
            logger.error(f"Failed to send hello: {e}")
            await websocket.close(code=4500)
            return
        logger.info(f"[TIMING] hello handshake took {time.time()-_hello_start:.3f}s")

        # Don't send capture-pane history on initial connect
        # Default mode is "tail" which uses lightweight JSON updates
        # History will be sent as catchup when client switches to "full" mode
        # Just send clear screen to trigger client overlay hide
        await websocket.send_text("\x1b[2J\x1b[H")
        logger.info(f"[TIMING] WebSocket setup TOTAL took {time.time()-_ws_start:.3f}s")

        # Track PTY death for proper close code
        pty_died = False
        # Track connection closed to prevent send-after-close errors
        connection_closed = False

        # Client output mode: "tail" (default) or "full"
        # In tail mode: don't forward raw PTY bytes, send periodic tail snapshots
        # In full mode: forward raw PTY bytes for full terminal rendering
        client_mode = "tail"
        # Ring buffer for recent output (for tail extraction and mode switch catchup)
        recent_buffer = bytearray()
        RECENT_BUFFER_MAX = 64 * 1024  # 64KB of recent output
        # Tail state
        tail_seq = 0
        TAIL_INTERVAL = 0.2  # Send tail updates every 200ms
        # Shared PTY output batch (cleared on mode switch to prevent stale data)
        pty_batch = bytearray()
        pty_batch_flush_time = time.time()

        # Create tasks for bidirectional I/O
        async def read_from_terminal():
            """Read from terminal and send to WebSocket with batching.

            CRITICAL: PTY is ALWAYS drained regardless of client_mode.
            - In 'full' mode: forward raw bytes to WebSocket (coalesced)
            - In 'tail' mode: skip WebSocket send (tail_sender handles updates)

            Coalescing: accumulate PTY bytes and flush every 25ms or 16KB
            to reduce WS message frequency and client pressure.
            """
            nonlocal pty_died, connection_closed, recent_buffer, pty_batch, pty_batch_flush_time
            loop = asyncio.get_event_loop()
            # Coalescing parameters - balance latency vs throughput
            # Aggressive rate limiting for mobile debugging
            FLUSH_INTERVAL = 0.200  # 200ms = 5 FPS max
            FLUSH_MAX_BYTES = 2048   # 2KB max per message (10KB/s total)

            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    # Non-blocking read with select-like behavior
                    # ALWAYS read - never pause PTY drain
                    data = await loop.run_in_executor(
                        None, lambda: os.read(master_fd, 4096)
                    )
                    if not data:
                        # PTY returned EOF - terminal died
                        logger.warning("PTY returned EOF - terminal process died")
                        pty_died = True
                        break

                    # Update input queue timestamp (for quiet-wait logic)
                    app.state.input_queue.update_output_ts()

                    # Store in ring buffer for future reconnects
                    output_buffer.write(data)

                    # Store in recent buffer for tail extraction and mode switch catchup
                    recent_buffer.extend(data)
                    if len(recent_buffer) > RECENT_BUFFER_MAX:
                        # Trim to last RECENT_BUFFER_MAX bytes
                        recent_buffer = recent_buffer[-RECENT_BUFFER_MAX:]

                    # Only forward to WebSocket in 'full' mode
                    if client_mode == "full":
                        # Add to batch (cap size to prevent memory issues)
                        pty_batch.extend(data)
                        if len(pty_batch) > FLUSH_MAX_BYTES * 4:
                            # Drop old data if accumulating too fast
                            # Keep from a UTF-8 safe boundary
                            keep_from = len(pty_batch) - FLUSH_MAX_BYTES
                            # Find start of a valid UTF-8 character
                            while keep_from < len(pty_batch) and (pty_batch[keep_from] & 0xC0) == 0x80:
                                keep_from += 1
                            pty_batch = pty_batch[keep_from:]

                        # ONLY flush on time interval (enforces rate limit for mobile)
                        # This prevents flooding the client even if PTY is very active
                        now = time.time()
                        if (now - pty_batch_flush_time) >= FLUSH_INTERVAL:
                            if app.state.active_websocket == websocket and pty_batch and not connection_closed:
                                # Send at most FLUSH_MAX_BYTES per interval
                                # Use UTF-8 safe boundary to avoid splitting multi-byte chars
                                cut_pos = find_utf8_boundary(pty_batch, FLUSH_MAX_BYTES)
                                send_data = bytes(pty_batch[:cut_pos])
                                pty_batch = pty_batch[cut_pos:]
                                await websocket.send_bytes(send_data)
                                pty_batch_flush_time = now

                except Exception as e:
                    # Ignore send-after-close errors (expected during disconnect)
                    if connection_closed or "after sending" in str(e) or "websocket.close" in str(e):
                        break
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error reading from terminal: {e}")
                    break

            # Flush remaining data (only in full mode)
            if client_mode == "full" and pty_batch and app.state.active_websocket == websocket and not connection_closed:
                try:
                    await websocket.send_bytes(bytes(pty_batch))
                except Exception:
                    pass

        async def write_to_terminal():
            """Read from WebSocket and write to terminal."""
            nonlocal client_mode, pty_batch, pty_batch_flush_time
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    message = await websocket.receive()

                    # Check message type safely
                    if not isinstance(message, dict):
                        continue

                    msg_type = message.get("type", "")
                    if msg_type == "websocket.disconnect":
                        break

                    if "bytes" in message:
                        os.write(master_fd, message["bytes"])
                        app.state.last_ws_input_time = time.time()
                    elif "text" in message:
                        text = message["text"]
                        logger.info(f"Received text message: {text[:100]}")
                        # Handle JSON messages (resize, input)
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                msg_type = data.get("type")
                                if msg_type == "resize":
                                    cols = data.get("cols", 80)
                                    rows = data.get("rows", 24)
                                    logger.info(f"Resize request: {cols}x{rows}, fd={master_fd}, pid={app.state.child_pid}")
                                    set_terminal_size(
                                        master_fd,
                                        cols,
                                        rows,
                                        app.state.child_pid,
                                    )
                                    logger.info(f"Terminal resized to {cols}x{rows}")
                                elif msg_type == "input":
                                    input_data = data.get("data")
                                    if input_data:
                                        os.write(master_fd, input_data.encode())
                                        app.state.last_ws_input_time = time.time()
                                elif msg_type == "ping":
                                    # Respond to heartbeat ping with pong
                                    if not connection_closed:
                                        await websocket.send_json({"type": "pong"})
                                elif msg_type == "pong":
                                    # Client responding to server_ping - connection is alive
                                    pass  # No action needed, connection confirmed alive
                                elif msg_type == "text":
                                    # Atomic text send via tmux send-keys (not PTY write)
                                    # This avoids interleaving with PTY output stream
                                    text_data = data.get("text", "")
                                    send_enter = data.get("enter", False)
                                    loop = asyncio.get_event_loop()
                                    session = app.state.current_session
                                    target = app.state.active_target
                                    tmux_t = get_tmux_target(session, target)
                                    if text_data:
                                        try:
                                            await loop.run_in_executor(
                                                None,
                                                lambda: subprocess.run(
                                                    ["tmux", "send-keys", "-t", tmux_t, "-l", text_data],
                                                    timeout=3, check=True,
                                                ),
                                            )
                                        except Exception as e:
                                            logger.warning(f"tmux send-keys failed: {e}")
                                    if send_enter:
                                        try:
                                            await loop.run_in_executor(
                                                None,
                                                lambda: subprocess.run(
                                                    ["tmux", "send-keys", "-t", tmux_t, "Enter"],
                                                    timeout=3, check=True,
                                                ),
                                            )
                                        except Exception as e:
                                            logger.warning(f"tmux send-keys Enter failed: {e}")
                                    app.state.last_ws_input_time = time.time()
                                elif msg_type == "set_mode":
                                    # Client requests output mode change
                                    new_mode = data.get("mode", "tail")
                                    if new_mode in ("tail", "full"):
                                        old_mode = client_mode
                                        client_mode = new_mode
                                        logger.info(f"[MODE] {old_mode} -> {client_mode}")
                                        # When switching to full mode:
                                        # Send capture-pane snapshot as immediate catchup,
                                        # then SIGWINCH for live forwarding.
                                        # The snapshot fixes the race where resize SIGWINCH
                                        # fires while still in tail mode (data lost).
                                        if new_mode == "full" and not connection_closed:
                                            pty_batch.clear()
                                            pty_batch_flush_time = 0
                                            # Send capture-pane snapshot so client has
                                            # current screen content immediately
                                            try:
                                                session = app.state.current_session
                                                target = app.state.active_target
                                                snapshot = await asyncio.get_event_loop().run_in_executor(
                                                    None,
                                                    lambda: subprocess.run(
                                                        ["tmux", "capture-pane", "-p", "-e", "-t",
                                                         get_tmux_target(session, target)],
                                                        capture_output=True, text=True, timeout=2,
                                                    ).stdout or ""
                                                )
                                                if snapshot and not connection_closed:
                                                    # Clear screen + send snapshot for clean render
                                                    await websocket.send_text("\x1b[2J\x1b[H" + snapshot)
                                                    logger.info(f"[MODE] Sent capture-pane snapshot ({len(snapshot)} bytes)")
                                            except Exception as e:
                                                logger.warning(f"[MODE] capture-pane catchup failed: {e}")
                                            # Also send SIGWINCH so live PTY forwarding
                                            # picks up from the correct state
                                            if app.state.child_pid:
                                                try:
                                                    os.kill(app.state.child_pid, signal.SIGWINCH)
                                                except ProcessLookupError:
                                                    pass
                                            logger.info("[MODE] Full mode activated with snapshot catchup")
                                elif msg_type == "term_subscribe":
                                    # Legacy: treat as set_mode full
                                    client_mode = "full"
                                    logger.info("Client subscribed to terminal view (mode=full)")
                                    if not connection_closed:
                                        pty_batch.clear()
                                        pty_batch_flush_time = time.time()
                                elif msg_type == "term_unsubscribe":
                                    # Legacy: treat as set_mode tail
                                    client_mode = "tail"
                                    logger.info("Client unsubscribed from terminal view (mode=tail)")
                            else:
                                # JSON but not dict, treat as plain text
                                os.write(master_fd, text.encode())
                        except (json.JSONDecodeError, TypeError, KeyError):
                            # Plain text input
                            os.write(master_fd, text.encode())

                except WebSocketDisconnect:
                    break
                except (OSError, IOError) as e:
                    # Terminal write errors are fatal (terminal closed, etc.)
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error writing to terminal: {e}")
                    break
                except Exception as e:
                    # Log but continue on other errors (malformed messages, etc.)
                    if app.state.active_websocket == websocket:
                        logger.warning(f"Ignoring malformed message: {e}")
                    continue

        async def server_keepalive():
            """Send periodic pings from server to keep connection alive."""
            SERVER_PING_INTERVAL = 20  # Send ping every 20s from server
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    await asyncio.sleep(SERVER_PING_INTERVAL)
                    if app.state.active_websocket == websocket and not connection_closed:
                        # Send server-initiated ping (client will respond with pong)
                        await websocket.send_json({"type": "server_ping"})
                except Exception:
                    break

        async def tail_sender():
            """Send periodic tail updates when in tail mode.

            Extracts last ~50 lines from recent_buffer, strips ANSI,
            and sends as JSON for lightweight Log view rendering.
            Also checks for pending permission requests (v2 messages).
            """
            nonlocal tail_seq
            perm_check_counter = 0
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    await asyncio.sleep(TAIL_INTERVAL)
                    if client_mode == "tail" and recent_buffer and not connection_closed:
                        # Extract last portion and decode
                        try:
                            text = bytes(recent_buffer[-8192:]).decode('utf-8', errors='replace')
                            # Strip ANSI and get last 50 lines
                            plain = strip_ansi(text)
                            lines = plain.split('\n')[-50:]
                            tail_text = '\n'.join(lines)
                            tail_seq += 1
                            await websocket.send_json({
                                "type": "tail",
                                "text": tail_text,
                                "seq": tail_seq
                            })
                        except Exception as e:
                            if "websocket.close" in str(e) or "after sending" in str(e):
                                break
                            logger.debug(f"Tail extraction error: {e}")

                    # Check for permission requests every ~1s (5 ticks at 200ms)
                    perm_check_counter += 1
                    if perm_check_counter >= 5 and not connection_closed:
                        perm_check_counter = 0
                        try:
                            detector = app.state.permission_detector
                            session = app.state.current_session
                            target = app.state.active_target
                            if session and detector.log_file:
                                tmux_t = get_tmux_target(session, target)
                                perm = await asyncio.get_event_loop().run_in_executor(
                                    None, detector.check_sync, session, target, tmux_t
                                )
                                if perm:
                                    await send_typed(websocket, "permission_request", perm, level="urgent")
                        except Exception as e:
                            logger.debug(f"Permission check error: {e}")
                except Exception:
                    break

        async def desktop_activity_monitor():
            """Detect desktop keyboard activity in tmux session (1.5s polling)."""
            last_hash = 0
            desktop_active = False
            desktop_since = 0
            while app.state.active_websocket == websocket and not connection_closed:
                await asyncio.sleep(1.5)
                try:
                    session = app.state.current_session
                    target = app.state.active_target
                    if not session:
                        continue
                    tmux_target = get_tmux_target(session, target)
                    def _capture_desktop():
                        r = subprocess.run(
                            ["tmux", "capture-pane", "-t", tmux_target, "-p", "-S", "-5"],
                            capture_output=True, text=True, timeout=1
                        )
                        return r.stdout
                    stdout = await asyncio.get_event_loop().run_in_executor(
                        None, _capture_desktop
                    )
                    current_hash = hash(stdout)
                    if current_hash != last_hash:
                        last_hash = current_hash
                        time_since_ws = time.time() - app.state.last_ws_input_time
                        if time_since_ws > 1.5 and not desktop_active:
                            desktop_active = True
                            desktop_since = time.time()
                            await send_typed(websocket, "device_state",
                                             {"desktop_active": True}, level="info")
                        elif time_since_ws <= 1.5 and desktop_active:
                            desktop_active = False
                            await send_typed(websocket, "device_state",
                                             {"desktop_active": False}, level="info")
                    if desktop_active and (time.time() - desktop_since) > 10:
                        desktop_active = False
                        await send_typed(websocket, "device_state",
                                         {"desktop_active": False}, level="info")
                except Exception:
                    pass

        # Run all tasks concurrently
        read_task = asyncio.create_task(read_from_terminal())
        app.state.read_task = read_task
        write_task = asyncio.create_task(write_to_terminal())
        keepalive_task = asyncio.create_task(server_keepalive())
        tail_task = asyncio.create_task(tail_sender())
        desktop_task = asyncio.create_task(desktop_activity_monitor())

        try:
            await asyncio.gather(read_task, write_task, keepalive_task, tail_task, desktop_task)
        except asyncio.CancelledError:
            # Normal termination when connection is replaced or closed
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            # Signal tasks to stop sending before canceling
            connection_closed = True
            read_task.cancel()
            write_task.cancel()
            keepalive_task.cancel()
            tail_task.cancel()
            desktop_task.cancel()
            if app.state.active_websocket == websocket:
                app.state.active_websocket = None
                app.state.read_task = None

            # Close with appropriate code
            if pty_died:
                logger.warning("Closing WebSocket with code 4500 (PTY died)")
                try:
                    await websocket.close(code=4500, reason="PTY died")
                except Exception:
                    pass
                # Clear PTY state so next connection recreates it
                app.state.master_fd = None
                app.state.child_pid = None

            logger.info("WebSocket connection closed")

    # ===== Push Notifications =====
    PUSH_DIR = Path.home() / ".mobile-terminal"
    PUSH_SUBS_FILE = PUSH_DIR / "push_subs.json"

    def load_push_subscriptions() -> list:
        if PUSH_SUBS_FILE.exists():
            try:
                return json.loads(PUSH_SUBS_FILE.read_text())
            except Exception:
                return []
        return []

    def save_push_subscriptions(subs: list):
        PUSH_DIR.mkdir(parents=True, exist_ok=True)
        PUSH_SUBS_FILE.write_text(json.dumps(subs, indent=2))

    _push_cooldowns: dict = {}

    async def maybe_send_push(title: str, body: str, push_type: str = "info", extra_data: dict = None):
        """Send push only if no active client and cooldown expired."""
        if not config.push_enabled:
            return
        if app.state.active_websocket is not None:
            return
        cooldowns = {"permission": 30, "completed": 300, "crashed": 60}
        min_interval = cooldowns.get(push_type, 30)
        now = time.time()
        if now - _push_cooldowns.get(push_type, 0) < min_interval:
            return
        subs = load_push_subscriptions()
        if not subs:
            return
        vapid_key_path = getattr(app.state, 'vapid_key_path', None)
        if not vapid_key_path:
            return
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            return
        payload = {"title": title, "body": body, "type": push_type}
        if extra_data:
            payload.update(extra_data)
        stale = []
        for sub in subs:
            try:
                webpush(sub, json.dumps(payload),
                        vapid_private_key=str(vapid_key_path),
                        vapid_claims={"sub": "mailto:noreply@localhost"})
            except WebPushException as e:
                if "410" in str(e) or "404" in str(e):
                    stale.append(sub.get('endpoint', ''))
            except Exception:
                pass
        if stale:
            subs = [s for s in subs if s.get('endpoint', '') not in stale]
            save_push_subscriptions(subs)
        _push_cooldowns[push_type] = now

    @app.get("/api/push/vapid-key")
    async def get_vapid_key(_auth=Depends(verify_token)):
        pub_key = getattr(app.state, 'vapid_public_key', None)
        if not pub_key:
            return JSONResponse({"error": "Push not configured"}, status_code=503)
        return {"key": pub_key}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request, _auth=Depends(verify_token)):
        sub = await request.json()
        subs = load_push_subscriptions()
        subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
        subs.append(sub)
        save_push_subscriptions(subs)
        return {"ok": True}

    @app.delete("/api/push/subscribe")
    async def push_unsubscribe(request: Request, _auth=Depends(verify_token)):
        sub = await request.json()
        subs = load_push_subscriptions()
        subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
        save_push_subscriptions(subs)
        return {"ok": True}

    # --- Router registration ---
    from mobile_terminal.routers import AppDeps
    from mobile_terminal.routers import context as context_router
    from mobile_terminal.routers import files as files_router
    from mobile_terminal.routers import challenge as challenge_router
    from mobile_terminal.routers import runner as runner_router
    from mobile_terminal.routers import preview as preview_router

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
            url = f"http://localhost:{config.port}/"
            if config.host == "0.0.0.0":
                print(f"WARNING: Listening on all interfaces without auth!")
                print(f"         Use --no-auth only on trusted networks (e.g. Tailscale)")
        else:
            print(f"Token:   {app.state.token}")
            url = f"http://localhost:{config.port}/?token={app.state.token}"
        print(f"URL:     {url}")
        print(f"{'=' * 60}\n")

        # Start background push monitor
        if config.push_enabled and getattr(app.state, 'vapid_key_path', None):
            async def push_monitor():
                """Check for permission prompts, idle transitions, and crashes."""
                _perm_pending_since = 0
                _last_activity_time = time.time()
                _was_active_phase = False
                _was_agent_running = False
                _crash_candidate_since = 0
                _last_snap_time = 0.0  # Rate-limit auto snapshots
                driver = app.state.driver
                agent_name = driver.display_name()

                while True:
                    await asyncio.sleep(5)
                    try:
                        session = app.state.current_session
                        target = app.state.active_target
                        if not session:
                            continue

                        pane_target = f"{session}:{target}" if target else session
                        extra = {"session": session, "pane_id": target or ""}

                        # Use driver.observe() for all detection
                        ctx = _build_observe_context(target) if target else None
                        if ctx is None:
                            continue
                        obs = driver.observe(ctx)

                        agent_running = obs.running
                        current_phase = obs.phase
                        is_active = current_phase not in ("idle",)

                        # Track activity time from observation
                        if obs.active:
                            _last_activity_time = time.time()

                        # === Permission push (existing) ===
                        if app.state.active_websocket is None:
                            detector = app.state.permission_detector
                            if detector.log_file:
                                perm = detector.check_sync(session, target, ctx.tmux_target)
                                if perm:
                                    if _perm_pending_since == 0:
                                        _perm_pending_since = time.time()
                                    elif time.time() - _perm_pending_since > 10:
                                        await maybe_send_push(
                                            f"{agent_name} needs approval",
                                            f"Allow {perm['tool']}: {perm['target'][:80]}?",
                                            "permission",
                                            extra_data=extra,
                                        )
                                else:
                                    _perm_pending_since = 0

                        # === Completed push (idle transition) ===
                        if _was_active_phase and not is_active:
                            idle_duration = time.time() - _last_activity_time
                            if idle_duration > 20:
                                await maybe_send_push(
                                    f"{agent_name} finished",
                                    f"Turn complete in {pane_target}. Tap to review.",
                                    "completed",
                                    extra_data=extra,
                                )

                        # === Crashed push (process-tree check with debounce) ===
                        if _was_agent_running and not agent_running:
                            if _crash_candidate_since == 0:
                                _crash_candidate_since = time.time()
                            elif time.time() - _crash_candidate_since > 10:
                                # Confirm no output for 10s
                                if time.time() - _last_activity_time > 10:
                                    await maybe_send_push(
                                        f"{agent_name} crashed",
                                        f"{agent_name} stopped in {pane_target}. Tap to respawn.",
                                        "crashed",
                                        extra_data=extra,
                                    )
                                    _crash_candidate_since = 0
                        else:
                            _crash_candidate_since = 0

                        # === Auto-capture snapshots (event-driven, rate-limited) ===
                        now = time.time()
                        if (is_active and obs.active
                                and now - _last_snap_time > 30):
                            try:
                                phase_result = {
                                    "phase": obs.phase, "detail": obs.detail,
                                    "tool": obs.tool, "session": session,
                                    "pane_id": target or "",
                                }
                                _try_auto_snapshot(session, target, phase_result)
                                _last_snap_time = now
                            except Exception:
                                pass

                        _was_active_phase = is_active
                        _was_agent_running = agent_running

                    except Exception as e:
                        logger.debug(f"push_monitor error: {e}")
            app.state.push_monitor_task = asyncio.create_task(push_monitor())

    @app.on_event("shutdown")
    async def shutdown():
        """Cleanup on shutdown."""
        app.state.input_queue.stop()
        app.state.command_queue.stop()

        push_task = getattr(app.state, 'push_monitor_task', None)
        if push_task and not push_task.done():
            push_task.cancel()

        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
            except Exception:
                pass

    return app


def spawn_tmux(session_name: str) -> tuple:
    """
    Spawn a tmux session with a pty.

    Uses `tmux new -A -s <session>` which:
    - Creates the session if it doesn't exist
    - Attaches to it if it does exist

    Args:
        session_name: Name of the tmux session.

    Returns:
        Tuple of (master_fd, child_pid).
    """
    master_fd, slave_fd = pty.openpty()

    pid = os.fork()
    if pid == 0:
        # Child process
        os.setsid()

        # Set the slave PTY as the controlling terminal
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            pass  # May fail on some systems, but dup2 should still work

        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(master_fd)
        os.close(slave_fd)

        # Set TERM for tmux
        os.environ["TERM"] = "xterm-256color"

        # Execute tmux
        os.execvp("tmux", ["tmux", "new", "-A", "-s", session_name])
    else:
        # Parent process
        os.close(slave_fd)
        return master_fd, pid


def set_terminal_size(fd: int, cols: int, rows: int, child_pid: int = None) -> None:
    """
    Set terminal size using TIOCSWINSZ ioctl.

    Args:
        fd: File descriptor of the pty master.
        cols: Number of columns.
        rows: Number of rows.
        child_pid: Optional child process ID to send SIGWINCH for redraw.
    """
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    # Send SIGWINCH to trigger tmux redraw
    if child_pid:
        try:
            os.kill(child_pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass
