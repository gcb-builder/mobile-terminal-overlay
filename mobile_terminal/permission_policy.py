"""Permission auto-approval policy engine.

Evaluates normalized PermissionRequests against rules, risk classification,
and mode to produce structured PermissionDecisions.

Storage: ~/.mobile-terminal/permission-policy.json
Audit:   ~/.cache/mobile-overlay/permission-audit.jsonl
"""

import fnmatch
import json
import logging
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional

from mobile_terminal.models import (
    PermissionDecision,
    PermissionRequest,
    PermissionRule,
)

logger = logging.getLogger(__name__)

POLICY_DIR = Path.home() / ".mobile-terminal"
POLICY_FILE = POLICY_DIR / "permission-policy.json"
AUDIT_DIR = Path.home() / ".cache" / "mobile-overlay"
AUDIT_FILE = AUDIT_DIR / "permission-audit.jsonl"

# ── Risk classification ──────────────────────────────────────────────────

HIGH_RISK_PATTERNS = [
    r'\bsudo\b',
    r'\brm\s+(-\w*r|-\w*f)',
    r'git\s+push\s+--force',
    r'git\s+reset\s+--hard',
    r'git\s+clean\s+-f',
    r'curl\b.*\|\s*(ba)?sh',
    r'wget\b.*\|\s*(ba)?sh',
    r'\bchmod\s+777\b',
    r'\bmkfs\b',
    r'\bdd\s+if=',
    r'>\s*/dev/',
    r'\bkill\s+-9',
]

MEDIUM_RISK_PATTERNS = [
    r'\bgit\s+push\b',
    r'\bnpm\s+publish\b',
    r'\bdocker\b',
    r'\bpip\s+install\b(?!.*-e\s+\.)',
    r'\bnpm\s+install\s+-g',
]

SENSITIVE_PATH_PATTERNS = [
    r'\.env($|\.)',
    r'\.git/',
    r'package-lock\.json$',
    r'yarn\.lock$',
    r'pnpm-lock\.yaml$',
    r'Pipfile\.lock$',
    r'poetry\.lock$',
]

_high_re = [re.compile(p) for p in HIGH_RISK_PATTERNS]
_medium_re = [re.compile(p) for p in MEDIUM_RISK_PATTERNS]
_sensitive_re = [re.compile(p) for p in SENSITIVE_PATH_PATTERNS]


def classify_risk(tool: str, target: str) -> str:
    """Classify a permission request's risk level."""
    if tool == "Bash":
        for pat in _high_re:
            if pat.search(target):
                return "high"
        for pat in _medium_re:
            if pat.search(target):
                return "medium"
        return "low"

    if tool in ("Read", "Glob", "Grep"):
        return "low"

    if tool in ("Edit", "Write"):
        for pat in _sensitive_re:
            if pat.search(target):
                return "medium"
        return "low"

    return "medium"


def normalize_request(perm: dict, repo_path) -> PermissionRequest:
    """Convert raw detector output to a normalized PermissionRequest."""
    tool = perm.get("tool", "")
    target = perm.get("target", "")
    command = target if tool == "Bash" else None
    path = target if tool in ("Edit", "Write", "Read", "Glob", "Grep") else None
    risk = classify_risk(tool, target)
    return PermissionRequest(
        tool=tool,
        target=target,
        command=command,
        path=path,
        repo=str(repo_path or ""),
        risk=risk,
        perm_id=perm.get("id", ""),
    )


# ── Built-in default rules ──────────────────────────────────────────────

def _make_default(tool: str, matcher_type: str, matcher: str) -> PermissionRule:
    return PermissionRule(
        id=f"default_{tool}_{matcher_type}_{matcher}".replace(" ", "_"),
        tool=tool,
        matcher_type=matcher_type,
        matcher=matcher,
        scope="global",
        scope_value=None,
        action="allow",
        created_at=0,
        created_from="default",
    )


DEFAULT_RULES: List[PermissionRule] = [
    _make_default("Read", "tool_only", ""),
    _make_default("Glob", "tool_only", ""),
    _make_default("Grep", "tool_only", ""),
    _make_default("Bash", "command", "git status"),
    _make_default("Bash", "command", "git diff"),
    _make_default("Bash", "command", "git log"),
]


# ── Policy engine ────────────────────────────────────────────────────────

class PermissionPolicy:
    """Evaluates permission requests against rules and modes.

    Evaluation order:
    1. Mode override (manual → always prompt)
    2. Hard safety guard (high risk → always prompt unless unrestricted)
    3. Deny rules (session → repo → global)
    4. Allow rules (session → repo → global)
    5. Fallback → prompt
    """

    MODES = ("manual", "safe_auto", "session_auto", "unrestricted")

    def __init__(self):
        self.mode: str = "safe_auto"
        self._rules: List[PermissionRule] = []
        self._session_rules: List[PermissionRule] = []  # in-memory only

    def evaluate(self, req: PermissionRequest) -> PermissionDecision:
        """Evaluate a permission request and return a structured decision."""
        # 1. Mode override
        if self.mode == "manual":
            return PermissionDecision("prompt", "mode_manual", None, req.risk)

        # 2. Hard safety guard
        if req.risk == "high" and self.mode != "unrestricted":
            return PermissionDecision("prompt", "hard_guard", None, req.risk)

        # 3. Deny rules (most specific first)
        for rule in self._match_rules(req, action="deny"):
            return PermissionDecision("deny", f"{rule.scope}_rule", rule.id, req.risk)

        # 4. Allow rules (most specific first)
        for rule in self._match_rules(req, action="allow"):
            return PermissionDecision("allow", f"{rule.scope}_rule", rule.id, req.risk)

        # 5. Fallback
        return PermissionDecision("prompt", "no_match", None, req.risk)

    def _match_rules(self, req: PermissionRequest, action: str) -> Iterable[PermissionRule]:
        """Yield matching rules in specificity order: session -> repo -> global."""
        all_rules = self._session_rules + self._rules + DEFAULT_RULES
        for scope in ("session", "repo", "global"):
            for rule in all_rules:
                if rule.action != action or rule.scope != scope:
                    continue
                if rule.scope == "repo" and rule.scope_value != req.repo:
                    continue
                if rule.tool != "*" and rule.tool != req.tool:
                    continue
                if rule.matcher_type == "tool_only" or not rule.matcher:
                    # Empty matcher = match any use of this tool
                    yield rule
                elif rule.matcher_type == "command" and req.command:
                    if req.command == rule.matcher or req.command.startswith(rule.matcher + " "):
                        yield rule
                elif rule.matcher_type == "path" and req.path:
                    if fnmatch.fnmatch(req.path, rule.matcher):
                        yield rule

    # ── Rule management ──────────────────────────────────────────────────

    def add_rule(self, tool: str, matcher_type: str, matcher: str,
                 scope: str, scope_value: Optional[str], action: str,
                 created_from: str = "banner",
                 note: Optional[str] = None) -> PermissionRule:
        """Create and persist a new rule. Deduplicates against existing rules."""
        # Normalize empty matcher to tool_only
        if not matcher:
            matcher_type = "tool_only"
            matcher = ""

        # Check for existing duplicate
        all_rules = self._session_rules if scope == "session" else self._rules
        for existing in all_rules:
            if (existing.tool == tool and existing.matcher_type == matcher_type
                    and existing.matcher == matcher and existing.scope == scope
                    and existing.scope_value == scope_value
                    and existing.action == action):
                logger.debug("Duplicate rule skipped: %s %s:%s", tool, matcher_type, matcher)
                return existing

        # Also skip command/path rules if a broader tool_only rule already exists
        if matcher_type != "tool_only":
            for existing in all_rules + DEFAULT_RULES:
                if (existing.tool == tool and existing.matcher_type == "tool_only"
                        and existing.action == action
                        and (existing.scope == scope and existing.scope_value == scope_value
                             or existing.scope == "global")):
                    logger.debug("Redundant rule skipped (tool_only exists): %s %s:%s",
                                 tool, matcher_type, matcher)
                    return existing

        rule = PermissionRule(
            id=str(uuid.uuid4()),
            tool=tool,
            matcher_type=matcher_type,
            matcher=matcher,
            scope=scope,
            scope_value=scope_value,
            action=action,
            created_at=time.time(),
            created_from=created_from,
            note=note,
        )
        if scope == "session":
            self._session_rules.append(rule)
        else:
            self._rules.append(rule)
            self.save()
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a rule by ID. Returns True if found."""
        for i, rule in enumerate(self._rules):
            if rule.id == rule_id:
                self._rules.pop(i)
                self.save()
                return True
        for i, rule in enumerate(self._session_rules):
            if rule.id == rule_id:
                self._session_rules.pop(i)
                return True
        return False

    def list_rules(self) -> List[PermissionRule]:
        """Return all rules (defaults + persisted + session)."""
        return DEFAULT_RULES + self._rules + self._session_rules

    def set_mode(self, mode: str) -> None:
        """Set the policy mode."""
        if mode not in self.MODES:
            raise ValueError(f"Invalid mode: {mode}")
        self.mode = mode
        self.save()

    # ── Persistence ──────────────────────────────────────────────────────

    def load(self) -> None:
        """Load rules and mode from disk."""
        if not POLICY_FILE.exists():
            return
        try:
            data = json.loads(POLICY_FILE.read_text())
            self.mode = data.get("mode", "safe_auto")
            if self.mode not in self.MODES:
                self.mode = "safe_auto"
            self._rules = []
            for rd in data.get("rules", []):
                try:
                    self._rules.append(PermissionRule(
                        id=rd["id"],
                        tool=rd["tool"],
                        matcher_type=rd["matcher_type"],
                        matcher=rd["matcher"],
                        scope=rd["scope"],
                        scope_value=rd.get("scope_value"),
                        action=rd["action"],
                        created_at=rd.get("created_at", 0),
                        created_from=rd.get("created_from", "menu"),
                        note=rd.get("note"),
                    ))
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping invalid permission rule: %s", e)
            logger.info("Loaded permission policy: mode=%s, %d rules", self.mode, len(self._rules))
        except Exception as e:
            logger.warning("Failed to load permission policy: %s", e)

    def save(self) -> None:
        """Persist rules and mode to disk."""
        try:
            POLICY_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "mode": self.mode,
                "rules": [asdict(r) for r in self._rules],
            }
            POLICY_FILE.write_text(json.dumps(data, indent=2) + "\n")
        except Exception as e:
            logger.error("Failed to save permission policy: %s", e)

    # ── Audit logging ────────────────────────────────────────────────────

    def audit(self, req: PermissionRequest, decision: PermissionDecision) -> None:
        """Append an entry to the audit log."""
        entry = {
            "ts": time.time(),
            "repo": req.repo,
            "tool": req.tool,
            "target": req.target,
            "decision": decision.action,
            "reason": decision.reason,
            "rule_id": decision.rule_id,
            "mode": self.mode,
            "risk": decision.risk,
        }
        try:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug("Audit log write failed: %s", e)

    def read_audit(self, limit: int = 50) -> List[dict]:
        """Read recent audit entries, newest first."""
        if not AUDIT_FILE.exists():
            return []
        try:
            lines = AUDIT_FILE.read_text().strip().splitlines()
            entries = []
            for line in lines[-limit:]:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
            entries.reverse()
            return entries
        except Exception as e:
            logger.debug("Audit log read failed: %s", e)
            return []
