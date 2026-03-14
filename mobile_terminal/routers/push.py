"""Routes for push notifications."""
import json
import logging
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
        if app.state.active_websocket is not None:
            return
        cooldowns = {"permission": 30, "completed": 300, "crashed": 60, "context_high": 300}
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

                # Use driver.observe() for all detection
                ctx = deps.build_observe_context(target) if target else None
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

                # === Context high push ===
                if obs.context_pct is not None:
                    threshold = app.state.config.context_alert_threshold
                    if obs.context_pct >= threshold:
                        remaining = round(100 - obs.context_pct, 1)
                        await maybe_send_push(
                            f"{agent_name}: context {remaining}% remaining",
                            f"Context window {obs.context_pct:.0f}% used in {pane_target}.",
                            "context_high",
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

    # Expose for startup event to call
    app.state._push_monitor = push_monitor
