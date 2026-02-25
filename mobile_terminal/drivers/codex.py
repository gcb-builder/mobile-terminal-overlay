"""
Codex CLI agent driver.

Minimal driver for OpenAI's Codex CLI. Uses activity-based phase detection
since Codex doesn't produce JSONL logs or set pane_title signals.
"""

import logging
import re
from typing import Optional

from .base import BaseAgentDriver, Observation, ObserveContext

logger = logging.getLogger(__name__)

# Codex permission prompt patterns
_CODEX_PERMISSION_RE = re.compile(
    r'(?:Allow|Approve|Run|Execute).*\?',
    re.IGNORECASE,
)


class CodexDriver(BaseAgentDriver):
    """Driver for Codex CLI agent."""

    _agent_id = "codex"
    _display_name = "Codex CLI"
    _process_name = "codex"

    def capabilities(self) -> dict:
        return {
            "has_jsonl_logs": False,
            "has_permission_signal": False,  # heuristic only
            "has_phase_detection": False,
            "has_pane_title_signal": False,
        }

    def detect_permission_wait(self, ctx: ObserveContext, obs: Observation) -> None:
        """Check terminal output for Codex permission prompts."""
        snapshot = ctx.get_pane_snapshot(lines=10)
        if not snapshot:
            return
        for line in snapshot.strip().split('\n')[-5:]:
            if _CODEX_PERMISSION_RE.search(line):
                obs.phase = "waiting"
                obs.waiting_reason = "permission"
                obs.detail = line.strip()[:80]
                return

    def parse_progress(self, ctx: ObserveContext, obs: Observation) -> None:
        """Activity-based: working if running, idle otherwise."""
        if obs.running:
            obs.phase = "working"
            obs.detail = "Working..."
        elif obs.active:
            obs.phase = "working"
            obs.detail = "Active..."
        else:
            obs.phase = "idle"
