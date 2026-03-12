"""Team templates, role prompts, and spec validation for the team launcher."""

import re
import subprocess

# Valid roles — must match TEAM_ROLES keys in routers/team.py
VALID_ROLES = {"explorer", "planner", "executor", "reviewer"}

# ── Role startup prompts ────────────────────────────────────────────
# Derived from role, not duplicated per template.
# {goal} is substituted at launch time.

ROLE_PROMPTS = {
    "explorer": (
        "You are the explorer. Research the codebase, find relevant files, "
        "answer questions from the team. Goal: {goal}"
    ),
    "executor": (
        "You are the executor. Implement changes, write code, run builds. "
        "Goal: {goal}"
    ),
    "reviewer": (
        "You are the reviewer. Validate changes, run tests, inspect diffs, "
        "report risks. Goal: {goal}"
    ),
    "planner": (
        "You are the planner. Design the approach, write implementation plans. "
        "Goal: {goal}"
    ),
    "leader": (
        "You are the leader. Coordinate the team, split tasks, track progress. "
        "Read .claude/dispatch.md when it appears."
    ),
}

# ── Templates ───────────────────────────────────────────────────────

TEMPLATES = {
    "solo_reviewer": {
        "label": "Solo + Reviewer",
        "description": "One executor, one reviewer. Good for small features or bug fixes.",
        "requires_leader": False,
        "agents": [
            {"default_name": "a-impl", "default_role": "executor"},
            {"default_name": "a-review", "default_role": "reviewer"},
        ],
    },
    "research_implement": {
        "label": "Research + Implement",
        "description": "Explorer investigates, executor builds. Investigation then action.",
        "requires_leader": False,
        "agents": [
            {"default_name": "a-explore", "default_role": "explorer"},
            {"default_name": "a-impl", "default_role": "executor"},
        ],
    },
    "feature_delivery": {
        "label": "Feature Delivery",
        "description": "Full team for building a feature end-to-end.",
        "requires_leader": True,
        "agents": [
            {"default_name": "a-explore", "default_role": "explorer"},
            {"default_name": "a-impl", "default_role": "executor"},
            {"default_name": "a-review", "default_role": "reviewer"},
        ],
    },
    "bug_hunt": {
        "label": "Bug Hunt",
        "description": "Explorer finds the bug, executor fixes it.",
        "requires_leader": True,
        "agents": [
            {"default_name": "a-explore", "default_role": "explorer"},
            {"default_name": "a-impl", "default_role": "executor"},
        ],
    },
    "review_swarm": {
        "label": "Code Review",
        "description": "Multiple reviewers for thorough audit or code review.",
        "requires_leader": True,
        "agents": [
            {"default_name": "a-review1", "default_role": "reviewer"},
            {"default_name": "a-review2", "default_role": "reviewer"},
        ],
    },
    "refactor_validate": {
        "label": "Refactor + Validate",
        "description": "Executor refactors, reviewer validates safety.",
        "requires_leader": True,
        "agents": [
            {"default_name": "a-impl", "default_role": "executor"},
            {"default_name": "a-review", "default_role": "reviewer"},
        ],
    },
}

# tmux-safe name pattern: alphanumeric + dash, 1-30 chars
_TMUX_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,29}$")


def validate_team_spec(spec: dict) -> list[str]:
    """Validate a team launch spec. Returns list of error strings (empty = valid).

    spec should have:
      - session: str
      - agents: list of {"name": str, "role": str}
    """
    errors = []

    # Session
    session = spec.get("session")
    if not session:
        errors.append("Missing session name")
    else:
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                errors.append(f"tmux session '{session}' does not exist")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            errors.append("tmux not available or timed out")

    # Agents
    agents = spec.get("agents", [])
    if not agents:
        errors.append("No agents specified")
        return errors

    names_seen = set()
    leader_count = 0

    for i, agent in enumerate(agents):
        name = agent.get("name", "")
        role = agent.get("role", "")

        # tmux-safe name
        if not name:
            errors.append(f"Agent {i}: missing name")
        elif not _TMUX_NAME_RE.match(name):
            errors.append(
                f"Agent '{name}': name must be alphanumeric + dashes, 1-30 chars"
            )

        # Unique names
        if name in names_seen:
            errors.append(f"Agent '{name}': duplicate name")
        names_seen.add(name)

        # Valid role
        if role not in VALID_ROLES:
            errors.append(
                f"Agent '{name}': invalid role '{role}' "
                f"(must be one of {sorted(VALID_ROLES)})"
            )

        # Leader tracking
        if name == "leader":
            leader_count += 1

    if leader_count > 1:
        errors.append("Multiple agents named 'leader'")

    return errors
