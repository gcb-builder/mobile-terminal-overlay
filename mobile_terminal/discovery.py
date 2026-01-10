"""
Project context auto-discovery for Mobile Terminal Overlay.

Walks up the directory tree to find:
1. .mobile-terminal.yaml (explicit config)
2. CLAUDE.md (extract role prefixes)
3. .git/ (project root marker)
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config, RolePrefix, load_config


def discover_project_config(start_dir: Optional[Path] = None) -> Config:
    """
    Auto-discover project context by walking up from start_dir.

    Looks for:
    1. .mobile-terminal.yaml (explicit config)
    2. CLAUDE.md (extract role prefixes)
    3. .git/ (project root marker)

    Args:
        start_dir: Directory to start from. Defaults to cwd.

    Returns:
        Config with discovered values merged over defaults.
    """
    start = start_dir or Path.cwd()
    config = Config()

    # Walk up directory tree
    for parent in [start] + list(start.parents):
        # Check for explicit config
        config_file = parent / ".mobile-terminal.yaml"
        if config_file.exists():
            config = load_config(config_file)
            config.project_root = parent
            # If we found explicit config, don't continue walking
            # but still check for CLAUDE.md if no role prefixes
            if not config.role_prefixes:
                claude_md = parent / "CLAUDE.md"
                if claude_md.exists():
                    config.role_prefixes = extract_roles_from_claude_md(claude_md)
            return config

        # Check for CLAUDE.md (extract roles)
        claude_md = parent / "CLAUDE.md"
        if claude_md.exists() and not config.role_prefixes:
            config.role_prefixes = extract_roles_from_claude_md(claude_md)
            if config.project_root is None:
                config.project_root = parent

        # Check for git root
        if (parent / ".git").exists():
            if config.project_root is None:
                config.project_root = parent
            break

    return config


def extract_roles_from_claude_md(path: Path) -> List[RolePrefix]:
    """
    Parse CLAUDE.md for role definitions.

    Looks for patterns like:
    - **Planner Agent (A)**: plans only
    - **Implementer Agent (A / Claude Code)**: code changes only
    - **Reviewer Agent (A)**: review only
    - **Runner Agent (B1)**: execution only

    Args:
        path: Path to CLAUDE.md file.

    Returns:
        List of RolePrefix objects.
    """
    try:
        content = path.read_text()
    except Exception:
        return []

    # Match: **Role Name (context)**: description
    # e.g., **Planner Agent (A)**: plans only
    pattern = r'\*\*([^*]+(?:Agent|Role)[^*]*)\s*\([^)]+\)\*\*:'
    matches = re.findall(pattern, content, re.IGNORECASE)

    role_prefixes = []
    seen_labels = set()

    for match in matches:
        # Extract the role name (e.g., "Planner Agent" -> "Planner")
        role_name = match.strip()

        # Check if this looks like a relevant role
        role_keywords = ["planner", "implementer", "reviewer", "runner"]
        if not any(kw in role_name.lower() for kw in role_keywords):
            continue

        # Create short label (first word)
        label = role_name.split()[0] + ":"

        # Avoid duplicates
        if label in seen_labels:
            continue
        seen_labels.add(label)

        role_prefixes.append(RolePrefix(
            label=label,
            insert=label + " ",
        ))

    return role_prefixes


def find_config_file(start_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Find .mobile-terminal.yaml by walking up directory tree.

    Args:
        start_dir: Directory to start from. Defaults to cwd.

    Returns:
        Path to config file if found, None otherwise.
    """
    start = start_dir or Path.cwd()

    for parent in [start] + list(start.parents):
        config_file = parent / ".mobile-terminal.yaml"
        if config_file.exists():
            return config_file

        # Stop at git root
        if (parent / ".git").exists():
            break

    return None
