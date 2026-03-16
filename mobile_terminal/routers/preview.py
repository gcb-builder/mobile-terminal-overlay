"""Routes for preview service management."""
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from mobile_terminal.helpers import get_tmux_target

logger = logging.getLogger(__name__)

# Cache for preview config per repo
_preview_config_cache: Dict[str, dict] = {}
_preview_status_cache: Dict[str, dict] = {}
_preview_status_cache_time: float = 0
PREVIEW_STATUS_CACHE_TTL = 2.0  # seconds

DEV_LOG_DIR = Path.home() / ".cache" / "mobile-overlay" / "dev-logs"


def load_preview_config(repo_path: Optional[Path]) -> Optional[dict]:
    """Load preview.config.json from repo, with caching."""
    if not repo_path:
        return None

    cache_key = str(repo_path)
    config_file = repo_path / "preview.config.json"

    # Check if file exists
    if not config_file.exists():
        _preview_config_cache.pop(cache_key, None)
        return None

    # Check cache freshness by mtime
    try:
        mtime = config_file.stat().st_mtime
        cached = _preview_config_cache.get(cache_key)
        if cached and cached.get("_mtime") == mtime:
            return cached
    except Exception:
        pass

    # Load and parse
    try:
        content = config_file.read_text()
        config = json.loads(content)
        config["_mtime"] = mtime
        if len(_preview_config_cache) > 50:
            _preview_config_cache.clear()
        _preview_config_cache[cache_key] = config
        return config
    except Exception as e:
        logger.warning(f"Failed to load preview config: {e}")
        return None


async def check_service_health(port: int, path: str = "/", timeout: float = 1.5) -> dict:
    """Check if service is responding via TCP connect + optional HTTP probe."""
    import socket
    import httpx

    # First try TCP connect (fast baseline)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        if result != 0:
            return {"status": "stopped", "latency": None}
    except Exception:
        return {"status": "stopped", "latency": None}

    # TCP succeeded, try HTTP probe for more accurate status
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            start = time.time()
            resp = await client.get(f"http://127.0.0.1:{port}{path}")
            latency = int((time.time() - start) * 1000)
            return {
                "status": "running" if resp.status_code < 500 else "error",
                "latency": latency,
                "statusCode": resp.status_code
            }
    except httpx.ConnectError:
        return {"status": "stopped", "latency": None}
    except Exception as e:
        # TCP worked but HTTP failed - still likely running
        return {"status": "running", "latency": None, "note": "TCP only"}


def _dev_log_path(repo_path: Optional[Path], service_id: str) -> Path:
    """Build log file path for a preview service."""
    repo_name = repo_path.name if repo_path else "unknown"
    # Sanitize for filesystem
    safe_name = re.sub(r'[^\w\-]', '_', f"{repo_name}--{service_id}")
    return DEV_LOG_DIR / f"{safe_name}.log"


async def _start_pipe_pane(runtime, session: str, pane_id: Optional[str], log_path: Path):
    """Enable tmux pipe-pane to capture pane output to a log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    target = get_tmux_target(session, pane_id)
    try:
        await runtime.pipe_pane(target, f"cat > {log_path}")
        logger.info(f"Dev logging started: {log_path}")
    except Exception as e:
        logger.warning(f"Failed to start pipe-pane: {e}")


async def _stop_pipe_pane(runtime, session: str, pane_id: Optional[str]):
    """Disable tmux pipe-pane on a pane."""
    target = get_tmux_target(session, pane_id)
    try:
        await runtime.pipe_pane(target)
    except Exception:
        pass


def register(app: FastAPI, deps):
    """Register preview routes."""

    @app.get("/api/preview/config")
    async def get_preview_config(
        _auth=Depends(deps.verify_token),
    ):
        """Load preview.config.json from current repo."""

        repo_path = deps.get_current_repo_path()
        config = load_preview_config(repo_path)

        if not config:
            return {"services": [], "exists": False}

        # Strip internal fields
        return {
            "exists": True,
            "name": config.get("name", ""),
            "services": config.get("services", []),
            "tailscaleServe": config.get("tailscaleServe"),
        }

    @app.get("/api/preview/status")
    async def get_preview_status(
        _auth=Depends(deps.verify_token),
    ):
        """Check health of all configured preview services."""

        global _preview_status_cache_time

        # Throttle: return cached if fresh
        now = time.time()
        if now - _preview_status_cache_time < PREVIEW_STATUS_CACHE_TTL:
            return {"services": list(_preview_status_cache.values()), "cached": True}

        repo_path = deps.get_current_repo_path()
        config = load_preview_config(repo_path)

        if not config or not config.get("services"):
            return {"services": []}

        # Check each service
        results = []
        for svc in config.get("services", []):
            port = svc.get("port")
            health_path = svc.get("healthPath", "/")
            if not port:
                continue

            status = await check_service_health(port, health_path)
            result = {
                "id": svc.get("id"),
                "status": status.get("status", "unknown"),
                "latency": status.get("latency"),
            }
            results.append(result)
            _preview_status_cache[svc.get("id")] = result

        _preview_status_cache_time = now
        return {"services": results, "cached": False}

    @app.post("/api/preview/start")
    async def start_preview_service(
        service_id: str = Query(...),
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Start a preview service by sending its startCommand to PTY."""

        # Validate target (safety: must match current session/pane)
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Load config and find service
        repo_path = deps.get_current_repo_path()
        config = load_preview_config(repo_path)
        if not config:
            return JSONResponse({"error": "No preview config found"}, status_code=404)

        service = None
        for svc in config.get("services", []):
            if svc.get("id") == service_id:
                service = svc
                break

        if not service:
            return JSONResponse({"error": f"Service '{service_id}' not found"}, status_code=404)

        start_command = service.get("startCommand")
        if not start_command:
            return JSONResponse({"error": f"No startCommand for '{service_id}'"}, status_code=400)

        # Check if PTY is available
        runtime = app.state.runtime
        if not runtime.has_fd:
            return JSONResponse({"error": "No PTY available"}, status_code=400)

        # Enable dev logging via tmux pipe-pane
        log_path = _dev_log_path(repo_path, service_id)
        current_session = session or app.state.current_session
        current_pane = pane_id or getattr(app.state, 'active_target', None)
        await _start_pipe_pane(runtime, current_session, current_pane, log_path)

        # Send command to PTY
        try:
            runtime.write_command(start_command)
            app.state.audit_log.log("preview_start", {
                "service_id": service_id,
                "command": start_command,
                "log_file": str(log_path),
            })
            return {
                "success": True,
                "service_id": service_id,
                "command": start_command,
                "label": service.get("label", service_id),
                "log_file": str(log_path),
            }
        except Exception as e:
            logger.error(f"Preview start failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/preview/stop")
    async def stop_preview_service(
        service_id: str = Query(...),
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Stop a preview service by sending Ctrl+C to PTY."""

        # Validate target (safety: must match current session/pane)
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Check if PTY is available
        runtime = app.state.runtime
        if not runtime.has_fd:
            return JSONResponse({"error": "No PTY available"}, status_code=400)

        # Stop dev logging
        current_session = session or app.state.current_session
        current_pane = pane_id or getattr(app.state, 'active_target', None)
        await _stop_pipe_pane(runtime, current_session, current_pane)

        # Send Ctrl+C (0x03) to PTY
        try:
            runtime.pty_write(b'\x03')
            app.state.audit_log.log("preview_stop", {"service_id": service_id})
            return {"success": True, "service_id": service_id}
        except Exception as e:
            logger.error(f"Preview stop failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/preview/logs")
    async def get_preview_logs(
        service_id: str = Query(...),
        tail: int = Query(200, ge=1, le=5000),
        _auth=Depends(deps.verify_token),
    ):
        """Read dev log for a preview service (last N lines)."""
        repo_path = deps.get_current_repo_path()
        log_path = _dev_log_path(repo_path, service_id)

        if not log_path.exists():
            return {"content": "", "lines": 0, "exists": False}

        try:
            raw = log_path.read_text(errors="replace")
            lines = raw.splitlines()
            tail_lines = lines[-tail:]
            return {
                "content": "\n".join(tail_lines),
                "lines": len(tail_lines),
                "total_lines": len(lines),
                "exists": True,
                "log_file": str(log_path),
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/preview/logs/list")
    async def list_preview_logs(
        _auth=Depends(deps.verify_token),
    ):
        """List available dev log files."""
        if not DEV_LOG_DIR.exists():
            return {"logs": []}

        logs = []
        for f in sorted(DEV_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            logs.append({
                "filename": f.name,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
                "service_id": f.stem.split("--", 1)[-1] if "--" in f.stem else f.stem,
            })
        return {"logs": logs}

    @app.delete("/api/preview/logs")
    async def clear_preview_logs(
        service_id: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Clear dev logs. If service_id is provided, clear only that log."""
        if not DEV_LOG_DIR.exists():
            return {"cleared": 0}

        cleared = 0
        if service_id:
            repo_path = deps.get_current_repo_path()
            log_path = _dev_log_path(repo_path, service_id)
            if log_path.exists():
                log_path.unlink()
                cleared = 1
        else:
            for f in DEV_LOG_DIR.glob("*.log"):
                f.unlink()
                cleared += 1
        return {"cleared": cleared}
