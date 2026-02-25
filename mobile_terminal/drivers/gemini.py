"""
Gemini CLI agent driver.

Detects Gemini CLI state primarily via tmux pane_title, which Gemini sets
dynamically (ui.dynamicWindowTitle is on by default):

  ◇  Ready (folder)          → idle
  ✦  Working... (folder)     → working
  ✦  Reading files (folder)  → working (with detail)
  ⏲  Working... (folder)     → working (silent)
  ✋  Action Required (folder) → waiting/permission

Process detection: Gemini runs as `node bundle/gemini.js`, so we grep for
"gemini-cli" in process cmdlines rather than matching the binary name.

Config home: ~/.gemini/ (JSON format)
"""

import logging
import re
import subprocess
from typing import Optional

from .base import BaseAgentDriver, Observation, ObserveContext

logger = logging.getLogger(__name__)

# Gemini pane_title icons (Unicode)
_ICON_READY = "◇"
_ICON_WORKING = "✦"
_ICON_SILENT = "⏲"
_ICON_ACTION = "✋"

# Pattern to detect any Gemini title
_GEMINI_TITLE_RE = re.compile(
    r'[◇✦⏲✋]\s+.+\(.+\)|Gemini CLI',
)

# Fallback permission regex for pane snapshot (same as GenericDriver)
_PERMISSION_RE = re.compile(
    r'(?:Allow|Approve|Confirm|Accept|Permit|Authorize)'
    r'.*'
    r'(?:y/n|y/N|Y/n|\[y\]|\[n\]|\(yes/no\))',
    re.IGNORECASE,
)


class GeminiDriver(BaseAgentDriver):
    """Driver for Google Gemini CLI agent.

    Phase detection primarily via pane_title (dynamic window title).
    Permission detection via "Action Required" in pane_title.
    Fallback permission regex on pane snapshot.
    """

    _agent_id = "gemini"
    _display_name = "Gemini CLI"
    _process_name = "gemini"

    def capabilities(self) -> dict:
        return {
            "has_jsonl_logs": False,
            "has_permission_signal": True,
            "has_phase_detection": True,
            "has_pane_title_signal": True,
        }

    def is_running(self, ctx: ObserveContext, obs: Observation) -> None:
        """Detect Gemini CLI process.

        Gemini runs as `node bundle/gemini.js`, so the process name in ps is
        "node", not "gemini". We search for "gemini-cli" in the cmdline of
        children of the shell pid. Falls back to pane_title detection.
        """
        if not ctx.shell_pid:
            # No shell_pid — check pane_title as activity signal
            if ctx.pane_title and _GEMINI_TITLE_RE.search(ctx.pane_title):
                obs.running = True
                obs.active = True
            return

        try:
            proc_check = subprocess.run(
                ["pgrep", "-f", "gemini-cli", "-P", str(ctx.shell_pid)],
                capture_output=True, text=True, timeout=2,
            )
            if proc_check.returncode == 0 and proc_check.stdout.strip():
                pids = proc_check.stdout.strip().split("\n")
                if pids and pids[0].isdigit():
                    obs.running = True
                    obs.pid = int(pids[0])
                    return
        except Exception:
            pass

        # Fallback: also try matching just "gemini" (some installs)
        try:
            proc_check = subprocess.run(
                ["pgrep", "-f", "gemini", "-P", str(ctx.shell_pid)],
                capture_output=True, text=True, timeout=2,
            )
            if proc_check.returncode == 0 and proc_check.stdout.strip():
                pids = proc_check.stdout.strip().split("\n")
                if pids and pids[0].isdigit():
                    obs.running = True
                    obs.pid = int(pids[0])
                    return
        except Exception:
            pass

        # Pane title fallback: if title has Gemini indicators, treat as running
        if ctx.pane_title and _GEMINI_TITLE_RE.search(ctx.pane_title):
            obs.running = True
            obs.active = True

    def observe(self, ctx: ObserveContext) -> Observation:
        """Gemini observation: PID + pane_title phase detection."""
        obs = Observation(
            agent_type=self.id(),
            agent_name=self.display_name(),
        )

        # 1. PID detection
        self.is_running(ctx, obs)

        # 2. Classify from pane_title (primary signal)
        if ctx.pane_title:
            classified = self._classify_from_pane_title(ctx.pane_title, obs)
            if classified:
                return obs

        # 3. Fallback permission check via pane snapshot
        if obs.running and obs.waiting_reason is None:
            self._detect_permission_from_snapshot(ctx, obs)

        # 4. Default phase from running state
        if obs.waiting_reason is None:
            if obs.running:
                obs.phase = "working"
                obs.detail = obs.detail or "Working..."
            else:
                obs.phase = "idle"

        return obs

    def _classify_from_pane_title(self, pane_title: str, obs: Observation) -> bool:
        """Classify phase from Gemini's dynamic pane_title.

        Returns True if classification succeeded, False if title doesn't match
        Gemini patterns (so caller can use fallback).
        """
        if "Action Required" in pane_title or _ICON_ACTION in pane_title:
            obs.phase = "waiting"
            obs.waiting_reason = "permission"
            obs.detail = "Approval needed"
            return True

        if _ICON_WORKING in pane_title or _ICON_SILENT in pane_title:
            obs.phase = "working"
            obs.active = True
            obs.detail = _extract_title_detail(pane_title) or "Working..."
            return True

        if "Working" in pane_title:
            obs.phase = "working"
            obs.active = True
            obs.detail = _extract_title_detail(pane_title) or "Working..."
            return True

        if "Ready" in pane_title or _ICON_READY in pane_title:
            obs.phase = "idle"
            obs.detail = "Ready"
            return True

        if "Gemini CLI" in pane_title:
            # Legacy static title mode (dynamicWindowTitle disabled)
            obs.phase = "working" if obs.running else "idle"
            obs.detail = ""
            return True

        return False

    def _detect_permission_from_snapshot(
        self, ctx: ObserveContext, obs: Observation
    ) -> None:
        """Fallback: check pane snapshot for generic permission prompts."""
        snapshot = ctx.get_pane_snapshot(lines=10)
        if not snapshot:
            return
        for line in snapshot.strip().split('\n')[-5:]:
            if _PERMISSION_RE.search(line):
                obs.phase = "waiting"
                obs.waiting_reason = "permission"
                obs.detail = line.strip()[:80]
                return


def _extract_title_detail(pane_title: str) -> str:
    """Extract the descriptive text from a Gemini pane title.

    Gemini titles look like: "✦  Reading files (my-project)     ..."
    We extract "Reading files" — the text between the icon and the folder name.
    The title is padded to 80 chars, so we strip whitespace.
    """
    # Remove leading icon characters and whitespace
    text = pane_title.lstrip("◇✦⏲✋ \t")

    # Remove trailing folder name in parens and padding
    paren_idx = text.rfind("(")
    if paren_idx > 0:
        text = text[:paren_idx].strip()

    # "Working..." is the default — not very informative
    if text in ("Working...", "Working"):
        return ""

    return text[:60] if text else ""
