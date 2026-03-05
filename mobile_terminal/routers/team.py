"""Routes for team coordination (leader + agent panes)."""
import logging
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from mobile_terminal.drivers import ObserveContext
from mobile_terminal.helpers import (
    run_subprocess, get_tmux_target,
    get_cached_capture, set_cached_capture,
)

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register team coordination routes."""

    @app.get("/api/team/state")
    async def get_team_state(
        session: Optional[str] = Query(None),
        _auth=Depends(deps.verify_token),
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
            git_info = deps.get_git_info_cached(cwd_path)

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
        _auth=Depends(deps.verify_token),
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
        _auth=Depends(deps.verify_token),
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
        _auth=Depends(deps.verify_token),
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
            git_info = deps.get_git_info_cached(cwd_path)
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
