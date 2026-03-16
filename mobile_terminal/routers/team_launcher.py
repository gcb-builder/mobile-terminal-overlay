"""Routes for team launcher — create team windows, start Claude, assign roles."""

import asyncio
import logging
import re
import secrets
import shlex
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mobile_terminal.helpers import run_subprocess, save_team_roles
from mobile_terminal.team_templates import (
    TEMPLATES, ROLE_PROMPTS, validate_team_spec,
)

logger = logging.getLogger(__name__)


# ── Request models ──────────────────────────────────────────────────

class AgentSpec(BaseModel):
    name: str
    role: str


class LaunchRequest(BaseModel):
    goal: str = ""
    template: str = ""
    plan_filename: str = ""
    session: str = ""
    repo_path: str = ""
    agents: list[AgentSpec] = []
    auto_dispatch: bool = True
    dry_run: bool = False


# ── Helpers ─────────────────────────────────────────────────────────

async def _get_current_branch(repo_path: str) -> Optional[str]:
    """Get current git branch name, or None."""
    try:
        result = await run_subprocess(
            ["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


async def _window_exists(session: str, name: str) -> bool:
    """Check if a tmux window with this name exists."""
    try:
        result = await run_subprocess(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
            timeout=5,
        )
        if result.returncode == 0:
            return name in result.stdout.strip().split("\n")
    except Exception:
        pass
    return False


# ── Router ──────────────────────────────────────────────────────────

def register(app: FastAPI, deps):
    """Register team launcher routes."""

    @app.get("/api/team/templates")
    async def get_templates(_auth=Depends(deps.verify_token)):
        """Return available team templates with agent counts."""
        result = {}
        for key, tmpl in TEMPLATES.items():
            total = len(tmpl["agents"])
            if tmpl.get("requires_leader"):
                total += 1  # leader window
            result[key] = {
                **tmpl,
                "total_agents": total,
            }
        return result

    @app.post("/api/team/launch")
    async def launch_team(req: LaunchRequest, _auth=Depends(deps.verify_token)):
        """Launch a team using a phased pipeline with per-step results."""

        session = req.session or app.state.current_session
        repo_path = req.repo_path
        if not repo_path:
            # Try to get from current session's active pane
            try:
                r = await run_subprocess(
                    ["tmux", "display-message", "-t", session, "-p",
                     "#{pane_current_path}"],
                    timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    repo_path = r.stdout.strip()
            except Exception:
                pass

        agents = [a.model_dump() for a in req.agents]
        goal = req.goal

        # If plan provided, read it and optionally extract goal
        plan_content = None
        if req.plan_filename:
            safe_name = re.sub(r'[^\w\-\.]', '', req.plan_filename)
            plan_path = Path.home() / ".claude" / "plans" / safe_name
            if plan_path.is_file():
                try:
                    plan_content = plan_path.read_text(encoding="utf-8")
                    # Extract first heading as goal if goal not provided
                    if not goal and plan_content:
                        for line in plan_content.split("\n"):
                            if line.startswith("# "):
                                goal = line[2:].strip()
                                break
                except Exception as e:
                    logger.warning("Failed to read plan file: %s", e)

        steps = []
        agent_results = []

        # ── Phase 1: Validate ───────────────────────────────────────
        spec = {"session": session, "agents": agents}
        errors = validate_team_spec(spec)
        if errors:
            steps.append({"name": "validate", "ok": False, "error": "; ".join(errors)})
            return {"success": False, "steps": steps, "agents": []}
        steps.append({"name": "validate", "ok": True})

        # ── Phase 2: Check branch ───────────────────────────────────
        if repo_path:
            branch = await _get_current_branch(repo_path)
            if branch in ("main", "master"):
                steps.append({
                    "name": "check_branch", "ok": True,
                    "warning": f"Currently on '{branch}' — team will work on this branch",
                })
            else:
                steps.append({"name": "check_branch", "ok": True})
        else:
            steps.append({"name": "check_branch", "ok": True, "warning": "No repo path detected"})

        # Dry run stops here
        if req.dry_run:
            for a in agents:
                agent_results.append({"name": a["name"], "ok": True, "dry_run": True})
            return {"success": True, "dry_run": True, "steps": steps, "agents": agent_results}

        # ── Phase 3: Create windows ─────────────────────────────────
        windows_ok = True
        for agent in agents:
            name = agent["name"]
            try:
                if await _window_exists(session, name):
                    agent_results.append({"name": name, "ok": True, "skipped": True})
                    continue
                try:
                    await app.state.runtime.new_window(session, name, cwd=repo_path or "")
                    agent_results.append({"name": name, "ok": True})
                except RuntimeError as win_err:
                    agent_results.append({
                        "name": name, "ok": False,
                        "error": str(win_err),
                    })
                    windows_ok = False
            except Exception as e:
                agent_results.append({"name": name, "ok": False, "error": str(e)})
                windows_ok = False

        if not windows_ok:
            steps.append({"name": "create_windows", "ok": False,
                          "error": "Some windows failed to create"})
        else:
            steps.append({"name": "create_windows", "ok": True})

        # ── Phase 4: Start agent ─────────────────────────────────────
        driver = app.state.driver
        agent_ok = True
        for ar in agent_results:
            if not ar["ok"] or ar.get("skipped"):
                continue
            name = ar["name"]
            try:
                target = f"{session}:{name}"
                cmd_parts = driver.start_command()
                cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)
                await app.state.runtime.send_keys(target, cmd_str, "Enter")
            except Exception as e:
                ar["ok"] = False
                ar["error"] = f"Failed to start {driver.display_name()}: {e}"
                agent_ok = False

        if not agent_ok:
            steps.append({"name": "start_agent", "ok": False,
                          "error": f"{driver.display_name()} failed to start in some windows"})
        else:
            steps.append({"name": "start_agent", "ok": True})

        # ── Phase 5: Await ready ────────────────────────────────────
        for ar in agent_results:
            if not ar["ok"] or ar.get("skipped"):
                continue
            name = ar["name"]
            ready = await driver.is_ready(session, name, timeout=15.0, interval=2.0)
            ar["ready"] = ready
            if not ready:
                ar["ready_status"] = "started_but_unconfirmed"

        steps.append({"name": "await_ready", "ok": True})

        # ── Phase 6: Inject role prompts ────────────────────────────
        prompts_ok = True
        for agent, ar in zip(agents, agent_results):
            if not ar["ok"] or ar.get("skipped"):
                continue
            name = agent["name"]
            role = agent["role"]
            prompt_template = ROLE_PROMPTS.get(role, "")
            if not prompt_template:
                continue
            prompt = prompt_template.format(goal=goal or "No specific goal set")
            try:
                target = f"{session}:{name}"
                await app.state.runtime.send_keys(target, prompt, literal=True)
                await app.state.runtime.send_keys(target, "Enter")
            except Exception as e:
                ar["prompt_error"] = str(e)
                prompts_ok = False

        if not prompts_ok:
            steps.append({"name": "inject_prompts", "ok": False,
                          "error": "Some prompts failed to inject"})
        else:
            steps.append({"name": "inject_prompts", "ok": True})

        # ── Phase 7: Save roles ─────────────────────────────────────
        try:
            roles_map = {a["name"]: a["role"] for a in agents}
            save_team_roles(roles_map)
            steps.append({"name": "save_roles", "ok": True})
        except Exception as e:
            steps.append({"name": "save_roles", "ok": False, "error": str(e)})

        # ── Phase 8: Dispatch (optional) ────────────────────────────
        if req.auto_dispatch and (plan_content or goal):
            try:
                # Find leader target
                leader_name = None
                for a in agents:
                    if a["name"] == "leader":
                        leader_name = "leader"
                        break
                if not leader_name:
                    # Use first agent if no leader
                    leader_name = agents[0]["name"]

                leader_target = f"{session}:{leader_name}"

                if plan_content and req.plan_filename:
                    # Use existing dispatch endpoint logic — find leader CWD and write dispatch.md
                    try:
                        cwd_result = await run_subprocess(
                            ["tmux", "display-message", "-t", leader_target,
                             "-p", "#{pane_current_path}"],
                            timeout=5,
                        )
                        leader_cwd = Path(cwd_result.stdout.strip()) if cwd_result.returncode == 0 else Path(repo_path)
                    except Exception:
                        leader_cwd = Path(repo_path)

                    dispatch_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
                    config_dir = driver.config_dir_name()

                    # Build simple dispatch content
                    dispatch_md = f"# Team Dispatch\n\n**ID:** {dispatch_id}\n**Goal:** {goal}\n\n"
                    dispatch_md += f"## Plan\n\n{plan_content}\n\n"

                    # Role roster
                    dispatch_md += "## Team Roster\n\n"
                    for a in agents:
                        dispatch_md += f"- **{a['name']}** — {a['role']}\n"
                    dispatch_md += "\n"

                    # Write dispatch file
                    dispatch_dir = leader_cwd / config_dir
                    dispatch_dir.mkdir(parents=True, exist_ok=True)
                    (dispatch_dir / "dispatch.md").write_text(dispatch_md, encoding="utf-8")

                    # Archive copy
                    archive_dir = dispatch_dir / "dispatch"
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    (archive_dir / f"dispatch-{dispatch_id}.md").write_text(dispatch_md, encoding="utf-8")

                    instruction = f"Read {config_dir}/dispatch.md and execute the plan. Dispatch ID: {dispatch_id}"
                else:
                    # Goal-only dispatch
                    instruction = goal

                # Send to leader
                await app.state.runtime.send_keys(leader_target, instruction, literal=True)
                await app.state.runtime.send_keys(leader_target, "Enter")
                steps.append({"name": "dispatch", "ok": True})

            except Exception as e:
                steps.append({"name": "dispatch", "ok": False, "error": str(e)})
        else:
            steps.append({"name": "dispatch", "ok": True, "skipped": True})

        # ── Result ──────────────────────────────────────────────────
        all_ok = all(s["ok"] for s in steps)
        return {"success": all_ok, "steps": steps, "agents": agent_results}

    @app.post("/api/team/kill")
    async def kill_team(
        session: str = "",
        _auth=Depends(deps.verify_token),
    ):
        """Kill all team windows in one batch to avoid cascading state changes."""
        sess = session or app.state.current_session

        # List windows
        result = await run_subprocess(
            ["tmux", "list-windows", "-t", sess, "-F", "#{window_name}"],
            timeout=5,
        )
        if result.returncode != 0:
            return JSONResponse(
                {"error": "Failed to list windows"}, status_code=500,
            )

        window_names = result.stdout.strip().split("\n")
        team_windows = [
            n.strip() for n in window_names
            if n.strip() and (n.strip() == "leader" or n.strip().startswith("a-"))
        ]

        if not team_windows:
            return {"success": True, "killed": [], "errors": []}

        # Kill all team windows in parallel to minimize state churn.
        # Skip /exit — kill-window is instant and avoids the 2s wait
        # that causes cascading re-renders as panes disappear one by one.
        kill_tasks = []
        for name in team_windows:
            kill_tasks.append(
                app.state.runtime.kill_window(f"{sess}:{name}")
            )
        results = await asyncio.gather(*kill_tasks, return_exceptions=True)

        killed = []
        errors = []
        for name, r in zip(team_windows, results):
            if isinstance(r, Exception):
                err_str = str(r)
                if "not found" in err_str.lower():
                    killed.append(name)  # Window already gone
                else:
                    errors.append(f"{name}: {r}")
            else:
                killed.append(name)

        # Clear roles
        try:
            save_team_roles({})
        except Exception:
            pass

        return {
            "success": len(errors) == 0,
            "killed": killed,
            "errors": errors,
        }
