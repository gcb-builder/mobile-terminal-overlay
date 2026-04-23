"""Routes for push notifications."""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

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


def register(app: FastAPI, deps):
    """Register push notification routes and background monitor."""

    _push_cooldowns: dict = {}

    async def maybe_send_push(title: str, body: str, push_type: str = "info", extra_data: dict = None):
        """Send push only if no active client and cooldown expired."""
        if not app.state.config.push_enabled:
            return
        if app.state.active_client is not None:
            return
        cooldowns = {"permission": 30, "completed": 300, "crashed": 60, "context_warn": 600, "context_high": 300}
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

    # Expose for use by other modules
    app.state._maybe_send_push = maybe_send_push

    @app.get("/api/push/vapid-key")
    async def get_vapid_key(_auth=Depends(deps.verify_token)):
        pub_key = getattr(app.state, 'vapid_public_key', None)
        if not pub_key:
            return JSONResponse({"error": "Push not configured"}, status_code=503)
        return {"key": pub_key}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request, _auth=Depends(deps.verify_token)):
        sub = await request.json()
        subs = load_push_subscriptions()
        subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
        subs.append(sub)
        save_push_subscriptions(subs)
        return {"ok": True}

    @app.delete("/api/push/subscribe")
    async def push_unsubscribe(request: Request, _auth=Depends(deps.verify_token)):
        sub = await request.json()
        subs = load_push_subscriptions()
        subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
        save_push_subscriptions(subs)
        return {"ok": True}

    async def push_monitor():
        """Check for permission prompts, idle transitions, and crashes."""
        import asyncio

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

                # Use driver.observe() for all detection (off event loop)
                ctx = await deps.build_observe_context(target) if target else None
                if ctx is None:
                    continue
                loop = asyncio.get_event_loop()
                obs = await loop.run_in_executor(None, driver.observe, ctx)

                agent_running = obs.running
                current_phase = obs.phase
                is_active = current_phase not in ("idle",)

                # Track activity time from observation
                if obs.active:
                    _last_activity_time = time.time()

                # === Permission push (with policy auto-approval) ===
                if app.state.active_client is None:
                    detector = app.state.permission_detector
                    if detector.log_file:
                        perm = detector.check_sync(session, target, ctx.tmux_target)
                        if perm:
                            # Evaluate policy before deciding to push
                            from mobile_terminal.permission_policy import normalize_request
                            policy = app.state.permission_policy
                            req = normalize_request(perm, deps.get_current_repo_path())
                            decision = policy.evaluate(req)
                            policy.audit(req, decision)

                            if decision.action == "allow":
                                runtime = app.state.runtime
                                await runtime.send_keys(ctx.tmux_target, "y", literal=True)
                                await runtime.send_keys(ctx.tmux_target, "Enter")
                                _perm_pending_since = 0
                            elif decision.action == "deny":
                                runtime = app.state.runtime
                                await runtime.send_keys(ctx.tmux_target, "n", literal=True)
                                await runtime.send_keys(ctx.tmux_target, "Enter")
                                _perm_pending_since = 0
                            else:
                                # Needs human — send push notification after delay
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

                # === Context push (warn at 70% used, critical at 85% used) ===
                if obs.context_pct is not None:
                    remaining = round(100 - obs.context_pct, 1)
                    if obs.context_pct >= app.state.config.context_alert_threshold:
                        await maybe_send_push(
                            f"{agent_name}: context {remaining}% remaining",
                            f"Context window {obs.context_pct:.0f}% used in {pane_target}. Consider compacting.",
                            "context_high",
                            extra_data=extra,
                        )
                    elif obs.context_pct >= app.state.config.context_warn_threshold:
                        await maybe_send_push(
                            f"{agent_name}: ctx {remaining}%",
                            f"Context window {obs.context_pct:.0f}% used in {pane_target}.",
                            "context_warn",
                            extra_data=extra,
                        )

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
                        deps.try_auto_snapshot(session, target, phase_result)
                        _last_snap_time = now
                    except Exception:
                        pass

                _was_active_phase = is_active
                _was_agent_running = agent_running

            except Exception as e:
                logger.debug(f"push_monitor error: {e}")

    # ── Multi-pane permission auto-approval ─────────────────────────────
    # Scans ALL panes for permission prompts and auto-approves based on
    # policy, independent of which pane the client is viewing.

    async def permission_scanner():
        """Scan all panes for permission prompts and auto-approve."""
        import asyncio
        from mobile_terminal.drivers.claude import ClaudePermissionDetector
        from mobile_terminal.permission_policy import normalize_request
        from mobile_terminal.helpers import get_tmux_target, get_project_id

        logger.info("[permission_scanner] Started — scanning all panes every 3s")

        # Per-pane detectors keyed by target_id
        detectors: dict[str, ClaudePermissionDetector] = {}
        # Track last-seen prompt per pane to avoid re-processing.
        # Lifted to app.state so the /api/terminal/text handler can bump
        # this when it forwards a banner-Allow y/n response — without
        # that, the scanner wouldn't know the user already answered and
        # could fire its own y on the now-cleared screen, which lands
        # as a stray turn after the agent is ready again.
        if not hasattr(app.state, "permission_scanner_cooldown"):
            app.state.permission_scanner_cooldown = {}
        last_approved: dict[str, float] = app.state.permission_scanner_cooldown

        def _scan_panes_sync(session: str) -> list:
            """Scan all panes for permission prompts. Runs in executor thread
            to avoid SIGCHLD handler conflicts with subprocess.run."""
            results = []
            try:
                list_result = subprocess.run(
                    ["tmux", "list-panes", "-s", "-t", session, "-F",
                     "#{window_index}:#{pane_index}|#{pane_current_path}|#{pane_id}"],
                    capture_output=True, text=True, timeout=3,
                )
                if list_result.returncode != 0 or not list_result.stdout.strip():
                    return results

                pane_lines = list_result.stdout.strip().split("\n")
                for line in pane_lines:
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue
                    target_id = parts[0]
                    pane_cwd = parts[1]
                    tmux_t = get_tmux_target(session, target_id)

                    # Skip if recently approved (cooldown 5s)
                    if time.time() - last_approved.get(target_id, 0) < 5:
                        continue

                    # Check for permission prompt in terminal
                    try:
                        cap = subprocess.run(
                            ["tmux", "capture-pane", "-p", "-t", tmux_t, "-S", "-15"],
                            capture_output=True, text=True, timeout=2,
                        )
                        pane_text = cap.stdout or ""
                        has_prompt = "do you want to proceed?" in pane_text.lower()
                        has_selector = re.search(r'[❯>]\s*\d+\.', pane_text) is not None
                        if not (has_prompt and has_selector):
                            continue
                    except Exception as e:
                        logger.debug(f"[permission_scanner] capture error {target_id}: {e}")
                        continue

                    logger.info(f"[permission_scanner] PROMPT DETECTED in {target_id} ({Path(pane_cwd).name})")

                    # Resolve JSONL log
                    repo_path = Path(pane_cwd)
                    project_id = get_project_id(repo_path)
                    claude_dir = Path.home() / ".claude" / "projects" / project_id
                    if not claude_dir.exists():
                        logger.info(f"[permission_scanner] no claude dir for {repo_path.name}: {claude_dir}")
                        continue

                    # Get or create per-pane detector
                    if target_id not in detectors:
                        detectors[target_id] = ClaudePermissionDetector()
                    det = detectors[target_id]

                    # Find log file
                    detect_fn = getattr(app.state, '_detect_target_log_file', None)
                    if not det.log_file and detect_fn:
                        log_file = detect_fn(target_id, session, claude_dir)
                        if log_file:
                            det.set_log_file(log_file)
                            logger.info(f"[permission_scanner] log file for {target_id}: {log_file.name}")
                        else:
                            logger.info(f"[permission_scanner] no log file found for {target_id}")
                            continue

                    # Extract tool info from JSONL
                    perm = det.check_sync(session, target_id, tmux_t)
                    if not perm:
                        # Re-scan recent entries with fresh detector
                        det2 = ClaudePermissionDetector()
                        det2.set_log_file(det.log_file)
                        try:
                            fsize = det.log_file.stat().st_size
                            det2.last_log_size = max(0, fsize - 10240)
                        except Exception:
                            pass
                        perm = det2.check_sync(session, target_id, tmux_t)
                        if perm:
                            det.last_log_size = det2.last_log_size
                            det.last_sent_id = det2.last_sent_id

                    if not perm:
                        # JSONL extraction failed — synthesize from terminal content.
                        # We know a permission prompt is showing. Extract tool name
                        # from the pane text (box header or prose).
                        tool_name = "Bash"  # default — most permission prompts are Bash
                        target_text = ""
                        for tl in pane_text.split('\n'):
                            stripped = tl.strip().replace('╭', '').replace('╮', '').replace('─', '').strip()
                            if stripped in ('Bash', 'Edit', 'Write', 'Read', 'Glob', 'Grep',
                                           'WebFetch', 'WebSearch', 'Agent', 'NotebookEdit'):
                                tool_name = stripped
                                break
                        perm = {
                            "tool": tool_name,
                            "target": target_text,
                            "context": "",
                            "id": f"scan:{target_id}:{int(time.time())}",
                        }
                        logger.info(f"[permission_scanner] synthesized perm for {target_id}: {tool_name}")

                    # Evaluate policy
                    policy = app.state.permission_policy
                    req = normalize_request(perm, repo_path)
                    decision = policy.evaluate(req)
                    policy.audit(req, decision)

                    results.append({
                        "target_id": target_id,
                        "tmux_t": tmux_t,
                        "repo_name": repo_path.name,
                        "perm": perm,
                        "req": req,
                        "decision": decision,
                    })
            except Exception as e:
                logger.warning(f"[permission_scanner] scan error: {e}", exc_info=True)
            return results

        scan_count = 0
        while True:
            await asyncio.sleep(3)
            try:
                session = app.state.current_session
                if not session:
                    continue
                scan_count += 1
                if scan_count <= 3 or scan_count % 100 == 0:
                    logger.info(f"[permission_scanner] scan #{scan_count}")

                loop = asyncio.get_event_loop()
                hits = await loop.run_in_executor(None, _scan_panes_sync, session)

                for hit in hits:
                    target_id = hit["target_id"]
                    tmux_t = hit["tmux_t"]
                    decision = hit["decision"]
                    perm = hit["perm"]
                    req = hit["req"]

                    if decision.action == "allow":
                        runtime = app.state.runtime
                        await runtime.send_keys(tmux_t, "y", literal=True)
                        await runtime.send_keys(tmux_t, "Enter")
                        last_approved[target_id] = time.time()
                        logger.info(f"[permission_scanner] Auto-approved {perm['tool']} "
                                    f"in {target_id} ({hit['repo_name']}): {decision.reason}")
                        sink = app.state.active_client
                        if sink:
                            await deps.send_typed(sink, "permission_auto",
                                {"decision": "allow", "tool": req.tool,
                                 "target": req.target, "reason": decision.reason,
                                 "pane": target_id, "repo": hit["repo_name"]},
                                level="info")
                    elif decision.action == "deny":
                        runtime = app.state.runtime
                        await runtime.send_keys(tmux_t, "n", literal=True)
                        await runtime.send_keys(tmux_t, "Enter")
                        last_approved[target_id] = time.time()
                        logger.info(f"[permission_scanner] Auto-denied {perm['tool']} "
                                    f"in {target_id}: {decision.reason}")
                    else:
                        logger.debug(f"[permission_scanner] Needs human: {perm['tool']} "
                                     f"in {target_id}: {decision.reason}")

            except Exception as e:
                logger.warning(f"[permission_scanner] loop error: {e}", exc_info=True)

    # Expose for startup event to call
    app.state._push_monitor = push_monitor
    app.state._permission_scanner = permission_scanner
