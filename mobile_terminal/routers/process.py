"""Routes for process management and agent health."""
import asyncio
import logging
import os
import signal
import subprocess
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from mobile_terminal.drivers import Observation
from mobile_terminal.helpers import run_subprocess

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register process management and agent routes."""

    @app.post("/api/process/terminate")
    async def terminate_process(
        _auth=Depends(deps.verify_token),
        force: bool = Query(False),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Terminate the PTY process.

        First tries SIGTERM, then SIGKILL if force=True or if SIGTERM fails.
        """

        # Validate target before destructive operation
        target_check = deps.validate_target(session, pane_id)
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
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Respawn the PTY process.

        Terminates existing process (if any) and creates a new one.
        """

        # Validate target before destructive operation
        target_check = deps.validate_target(session, pane_id)
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
            master_fd, child_pid = app.state.spawn_tmux(session_name)
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
        _auth=Depends(deps.verify_token),
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

    @app.get("/api/health/agent")
    @app.get("/api/health/claude")  # permanent alias
    async def check_agent_health(
        pane_id: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """
        Check agent status for a specific pane.

        Returns flat Observation JSON: agent_type, agent_name, running, pid,
        phase, detail, tool, active, waiting_reason, permission_tool, permission_target.
        """

        ctx = deps.build_observe_context(pane_id)
        if ctx is None:
            return Observation(
                agent_type=app.state.driver.id(),
                agent_name=app.state.driver.display_name(),
            ).to_dict()

        obs = app.state.driver.observe(ctx)
        return obs.to_dict()

    @app.get("/api/status/phase")
    async def get_status_phase(
        pane_id: str = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Get agent's current phase for the status strip. Delegates to driver."""

        target = pane_id or app.state.active_target
        ctx = deps.build_observe_context(target) if target else None

        if ctx is None:
            return {
                "phase": "idle", "detail": "", "tool": "",
                "session": app.state.current_session,
                "pane_id": target or "",
                "agent_running": False,
                "claude_running": False,  # deprecated alias
            }

        obs = app.state.driver.observe(ctx)
        return {
            "phase": obs.phase,
            "detail": obs.detail,
            "tool": obs.tool,
            "session": app.state.current_session,
            "pane_id": target or "",
            "agent_running": obs.running,
            "claude_running": obs.running,  # deprecated alias
        }

    @app.post("/api/agent/start")
    @app.post("/api/claude/start")  # permanent alias
    async def start_agent_in_pane(
        request: Request,
        pane_id: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """
        Start agent in a pane if not already running.

        Returns 409 if agent is already running.
        Uses driver.start_command() for the default, or repo's startup_command.
        """

        driver = app.state.driver
        agent_name = driver.display_name()

        # Check if agent is already running via driver.observe()
        ctx = deps.build_observe_context(pane_id)
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
            config = app.state.config
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
