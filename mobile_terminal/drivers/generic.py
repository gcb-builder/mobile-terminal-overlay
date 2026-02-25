"""
Generic agent driver — fallback for unknown agent types.

Uses heuristics:
- Permission: stdout regex (Allow|Approve|Confirm).*(y/n|[y/N])
- Phase: activity-based (working if recent output, idle otherwise)
- PID: any child of shell_pid
"""

import re
import logging

from .base import BaseAgentDriver, Observation, ObserveContext

logger = logging.getLogger(__name__)

# Common permission prompt patterns in terminal output
_PERMISSION_RE = re.compile(
    r'(?:Allow|Approve|Confirm|Accept|Permit|Authorize)'
    r'.*'
    r'(?:y/n|y/N|Y/n|\[y\]|\[n\]|\(yes/no\))',
    re.IGNORECASE,
)


class GenericDriver(BaseAgentDriver):
    """Fallback driver for unknown agents."""

    _agent_id = "generic"
    _display_name = "Agent"
    _process_name = ""

    def capabilities(self) -> dict:
        return {
            "has_jsonl_logs": False,
            "has_permission_signal": False,  # heuristic only
            "has_phase_detection": False,
            "has_pane_title_signal": False,
        }

    def detect_permission_wait(self, ctx: ObserveContext, obs: Observation) -> None:
        """Check terminal output for permission-like prompts."""
        snapshot = ctx.get_pane_snapshot(lines=10)
        if not snapshot:
            return
        # Check last few lines for permission prompts
        for line in snapshot.strip().split('\n')[-5:]:
            if _PERMISSION_RE.search(line):
                obs.phase = "waiting"
                obs.waiting_reason = "permission"
                obs.detail = line.strip()[:80]
                return
