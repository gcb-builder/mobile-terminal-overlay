"""Routes for process management and agent health."""
import asyncio
import logging
import os
import subprocess
import time as _time
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from mobile_terminal.drivers import Observation

logger = logging.getLogger(__name__)

# ── Pane descendant process helpers (Linux /proc) ────────────────────

_CLK_TCK = 100  # default; overwritten at import time
_PAGE_SIZE = 4096
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError):
    pass

_NOISE_SHELLS = frozenset({"bash", "sh", "zsh", "fish", "dash", "login", "sshd"})


def _read_boot_time() -> float:
    """Read system boot time from /proc/stat (btime line)."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def _parse_proc_stat(pid: int):
    """Parse /proc/{pid}/stat — returns (ppid, comm, state, utime, stime, starttime, rss_pages) or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
        # comm can contain parens/spaces — find the last ')' to delimit it
        i = raw.rfind(")")
        if i < 0:
            return None
        comm = raw[raw.index("(") + 1:i]
        fields = raw[i + 2:].split()
        # fields index: 0=state, 1=ppid, ..., 11=utime, 12=stime, ..., 19=starttime, ..., 21=rss
        return (
            int(fields[1]),   # ppid
            comm,
            fields[0],        # state
            int(fields[11]),  # utime
            int(fields[12]),  # stime
            int(fields[19]),  # starttime (clock ticks since boot)
            int(fields[21]),  # rss (pages)
        )
    except Exception:
        return None


def _build_pid_map():
    """Single pass over /proc — returns {pid: (ppid, comm, state, utime, stime, starttime, rss_pages)}."""
    pid_map = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            parsed = _parse_proc_stat(pid)
            if parsed:
                pid_map[pid] = parsed
    except Exception:
        pass
    return pid_map


def _descendants_bfs(shell_pid, pid_map):
    """BFS from shell_pid. Returns [(pid, depth), ...] excluding noise shells that have children."""
    # Build children-of index
    children_of = {}
    for pid, (ppid, *_) in pid_map.items():
        children_of.setdefault(ppid, []).append(pid)

    result = []
    queue = [(shell_pid, 0)]
    visited = {shell_pid}

    while queue:
        parent, depth = queue.pop(0)
        for child in children_of.get(parent, []):
            if child in visited:
                continue
            visited.add(child)
            info = pid_map.get(child)
            if not info:
                continue
            comm = info[1]
            has_kids = child in children_of
            # Skip intermediary shell wrappers (they have children to show instead)
            if comm in _NOISE_SHELLS and has_kids:
                queue.append((child, depth + 1))
                continue
            result.append((child, depth + 1))
            queue.append((child, depth + 1))

    return result, children_of


def _enumerate_descendants(shell_pid: int) -> dict:
    """Full descendant enumeration with per-process details. Runs in executor."""
    now = _time.time()
    boot_time = _read_boot_time()
    pid_map = _build_pid_map()

    if shell_pid not in pid_map:
        return {"shell_pid": shell_pid, "processes": [], "count": 0}

    desc_list, _ = _descendants_bfs(shell_pid, pid_map)

    processes = []
    for pid, depth in desc_list:
        info = pid_map.get(pid)
        if not info:
            continue
        ppid, comm, state, utime, stime, starttime, rss_pages = info

        # Elapsed time
        elapsed_s = now - boot_time - (starttime / _CLK_TCK)
        if elapsed_s < 0:
            elapsed_s = 0

        # Skip very short-lived processes
        if elapsed_s < 3:
            continue

        # Read full cmdline
        cmdline = ""
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmdline = f.read().replace("\x00", " ").strip()
        except Exception:
            pass
        if not cmdline:
            cmdline = comm
        if len(cmdline) > 200:
            cmdline = cmdline[:200] + "..."

        # Avg CPU% over lifetime
        total_ticks = utime + stime
        cpu_pct = round((total_ticks / _CLK_TCK) / max(elapsed_s, 0.1) * 100, 1)

        mem_mb = round(rss_pages * _PAGE_SIZE / 1048576, 1)

        processes.append({
            "pid": pid,
            "ppid": ppid,
            "name": comm,
            "command": cmdline,
            "state": state,
            "cpu_pct": cpu_pct,
            "mem_mb": mem_mb,
            "elapsed_s": round(elapsed_s),
            "depth": depth,
        })

    # Stable sort: depth asc, then PID asc
    processes.sort(key=lambda p: (p["depth"], p["pid"]))

    return {"shell_pid": shell_pid, "processes": processes, "count": len(processes)}


def _count_descendants(shell_pid: int) -> int:
    """Fast descendant count — same filtering as _enumerate_descendants but no cmdline/CPU reads."""
    now = _time.time()
    boot_time = _read_boot_time()
    pid_map = _build_pid_map()

    if shell_pid not in pid_map:
        return 0

    desc_list, _ = _descendants_bfs(shell_pid, pid_map)

    count = 0
    for pid, depth in desc_list:
        info = pid_map.get(pid)
        if not info:
            continue
        starttime = info[5]
        elapsed_s = now - boot_time - (starttime / _CLK_TCK)
        if elapsed_s < 3:
            continue
        count += 1

    return count


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

        runtime = app.state.runtime
        child_pid = runtime.child_pid
        if not child_pid:
            return JSONResponse({"error": "No process running"}, status_code=400)

        try:
            method = runtime.terminate(force=force)
            app.state.audit_log.log("process_terminate", {"pid": child_pid, "signal": method})
            runtime.close_fd()

            return {
                "success": True,
                "pid": child_pid,
                "method": method
            }
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

        runtime = app.state.runtime
        old_pid = runtime.child_pid

        # Terminate existing process if any
        if old_pid:
            try:
                runtime.terminate(force=True)
            except Exception as e:
                logger.warning(f"Error terminating old process: {e}")

        # Clean up old PTY
        runtime.close_fd()
        app.state.output_buffer.clear()

        # Spawn new PTY
        try:
            session_name = app.state.current_session
            master_fd, child_pid = runtime.spawn(session_name)

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

        runtime = app.state.runtime
        child_pid = runtime.child_pid
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
            "has_pty": runtime.has_fd,
            "session": app.state.current_session
        }

    # ========== End Process Management API ==========

    # ========== Pane Descendant Processes ==========

    @app.get("/api/process/children")
    async def process_children(
        pane_id: str = Query(...),
        _auth=Depends(deps.verify_token),
    ):
        """List descendant processes of the pane's shell PID.

        Walks /proc to enumerate all descendants, filters noise (shell
        wrappers, transient processes), returns meaningful background
        processes with CPU/mem/elapsed info.  Linux-only; returns empty
        list when /proc is unavailable.
        """
        ctx = await deps.build_observe_context(pane_id)
        if ctx is None or ctx.shell_pid is None:
            return {"shell_pid": None, "processes": [], "count": 0, "agent_pid": None}

        loop = asyncio.get_event_loop()
        # Detect agent PID so frontend can label it
        obs = await loop.run_in_executor(None, app.state.driver.observe, ctx)
        agent_pid = obs.pid if obs and obs.running else None

        result = await loop.run_in_executor(
            None, _enumerate_descendants, ctx.shell_pid
        )
        result["agent_pid"] = agent_pid
        return result

    # ========== System Metrics API ==========
    _prev_cpu = {"values": None, "time": 0}

    @app.get("/api/metrics")
    async def system_metrics(_auth=Depends(deps.verify_token)):
        """System resource metrics: CPU, memory, disk usage."""
        metrics = {}

        # CPU: delta from cached /proc/stat reading
        try:
            def _read_cpu():
                with open("/proc/stat") as f:
                    parts = f.readline().split()[1:]
                return [int(x) for x in parts]

            now_vals = _read_cpu()
            prev = _prev_cpu["values"]
            if prev and _time.time() - _prev_cpu["time"] < 30:
                delta = [b - a for a, b in zip(prev, now_vals)]
                total = sum(delta) or 1
                idle = delta[3] if len(delta) > 3 else 0
                metrics["cpu_pct"] = round((1 - idle / total) * 100, 1)
            else:
                metrics["cpu_pct"] = None
            _prev_cpu["values"] = now_vals
            _prev_cpu["time"] = _time.time()
        except Exception:
            metrics["cpu_pct"] = None

        # Memory: /proc/meminfo
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(":")] = int(parts[1])
            total = meminfo.get("MemTotal", 1)
            available = meminfo.get("MemAvailable", total)
            used = total - available
            metrics["mem_pct"] = round(used / max(total, 1) * 100, 1)
        except Exception:
            metrics["mem_pct"] = None

        # Disk: os.statvfs
        try:
            st = os.statvfs("/")
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            metrics["disk_pct"] = round(used / max(total, 1) * 100, 1)
        except Exception:
            metrics["disk_pct"] = None

        return metrics

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

        ctx = await deps.build_observe_context(pane_id)
        if ctx is None:
            return Observation(
                agent_type=app.state.driver.id(),
                agent_name=app.state.driver.display_name(),
            ).to_dict()

        loop = asyncio.get_event_loop()
        obs = await loop.run_in_executor(None, app.state.driver.observe, ctx)
        return obs.to_dict()

    @app.get("/api/status/phase")
    async def get_status_phase(
        pane_id: str = Query(None),
        _auth=Depends(deps.verify_token),
    ):
        """Get agent's current phase for the status strip. Delegates to driver."""

        target = pane_id or app.state.active_target
        ctx = await deps.build_observe_context(target) if target else None

        if ctx is None:
            return {
                "phase": "idle", "detail": "", "tool": "",
                "session": app.state.current_session,
                "pane_id": target or "",
                "agent_running": False,
                "claude_running": False,  # deprecated alias
                "context_used": None,
                "context_limit": None,
                "context_pct": None,
                "descendant_count": 0,
            }

        loop = asyncio.get_event_loop()
        obs = await loop.run_in_executor(None, app.state.driver.observe, ctx)
        desc_count = _count_descendants(ctx.shell_pid) if ctx.shell_pid else 0
        return {
            "phase": obs.phase,
            "detail": obs.detail,
            "tool": obs.tool,
            "session": app.state.current_session,
            "pane_id": target or "",
            "agent_running": obs.running,
            "claude_running": obs.running,  # deprecated alias
            "context_used": obs.context_used,
            "context_limit": obs.context_limit,
            "context_pct": obs.context_pct,
            "descendant_count": desc_count,
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
        ctx = await deps.build_observe_context(pane_id)
        if ctx is None:
            return JSONResponse({"error": "Pane not found"}, status_code=404)

        loop = asyncio.get_event_loop()
        obs = await loop.run_in_executor(None, driver.observe, ctx)
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
            await app.state.runtime.send_keys(pane_id, actual_cmd, literal=True)
            await app.state.runtime.send_keys(pane_id, "Enter")

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
