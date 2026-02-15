"""
Configuration system for Mobile Terminal Overlay.

Supports:
- YAML config files (.mobile-terminal.yaml)
- CLI argument overrides
- Sensible defaults
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class QuickCommand:
    """A quick command button."""
    label: str
    send: str  # Text to send (including \n if needed)


@dataclass
class RolePrefix:
    """A role prefix button."""
    label: str
    insert: str  # Text to insert into input field


@dataclass
class ContextButton:
    """A context button that prompts for values."""
    label: str
    template: str  # Template with {placeholders}
    prompt_fields: List[str] = field(default_factory=list)


@dataclass
class DeviceConfig:
    """Per-device overrides, keyed by Tailscale hostname."""
    font_size: Optional[int] = None
    physical_kb: bool = False


@dataclass
class Repo:
    """A repository/workspace configuration."""
    label: str  # Display name
    path: str  # Absolute path to repo
    session: str  # tmux session name
    startup_command: Optional[str] = None  # Command to run when auto_start enabled (default: "claude")
    startup_delay_ms: int = 300  # Delay before sending startup command (0..5000)


@dataclass
class Config:
    """Configuration for Mobile Terminal Overlay."""

    # Session settings
    session_name: str = "mobile-term"
    port: int = 8765
    host: str = "0.0.0.0"

    # Authentication (disabled by default, use --require-token to enable)
    token: Optional[str] = None  # Auto-generated if not set
    no_auth: bool = True  # Auth disabled by default (Tailscale-friendly)

    # Quick commands (sent on tap)
    quick_commands: List[QuickCommand] = field(default_factory=lambda: [
        QuickCommand(label="y", send="y\n"),
        QuickCommand(label="n", send="n\n"),
        QuickCommand(label="Enter", send="\n"),
    ])

    # Role prefixes (inserted, not sent)
    role_prefixes: List[RolePrefix] = field(default_factory=list)

    # Context buttons (prompt for values)
    context_buttons: List[ContextButton] = field(default_factory=list)

    # Repositories/workspaces for switching
    repos: List[Repo] = field(default_factory=list)

    # UI settings
    theme: str = "dark"  # dark | light | auto
    font_size: int = 16
    scrollback: int = 20000

    # Per-device overrides (keyed by Tailscale hostname)
    devices: Dict[str, DeviceConfig] = field(default_factory=dict)

    # Auto-setup: create/adopt tmux session on startup
    auto_setup: bool = True

    # Project context (set by discovery)
    project_root: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary for JSON serialization."""
        return {
            "session_name": self.session_name,
            "port": self.port,
            "host": self.host,
            "quick_commands": [
                {"label": cmd.label, "send": cmd.send}
                for cmd in self.quick_commands
            ],
            "role_prefixes": [
                {"label": rp.label, "insert": rp.insert}
                for rp in self.role_prefixes
            ],
            "context_buttons": [
                {"label": cb.label, "template": cb.template, "prompt_fields": cb.prompt_fields}
                for cb in self.context_buttons
            ],
            "repos": [
                {
                    "label": r.label,
                    "path": r.path,
                    "session": r.session,
                    "startup_command": r.startup_command,
                    "startup_delay_ms": r.startup_delay_ms,
                }
                for r in self.repos
            ],
            "theme": self.theme,
            "font_size": self.font_size,
            "scrollback": self.scrollback,
            "auto_setup": self.auto_setup,
        }

    def to_yaml(self) -> str:
        """Convert config to YAML string."""
        data = {
            "session_name": self.session_name,
            "port": self.port,
            "quick_commands": [
                {"label": cmd.label, "send": cmd.send}
                for cmd in self.quick_commands
            ],
            "role_prefixes": [
                {"label": rp.label, "insert": rp.insert}
                for rp in self.role_prefixes
            ],
            "context_buttons": [
                {"label": cb.label, "template": cb.template, "prompt_fields": cb.prompt_fields}
                for cb in self.context_buttons
            ],
            "theme": self.theme,
            "font_size": self.font_size,
            "scrollback": self.scrollback,
        }
        return yaml.dump(data, default_flow_style=False, sort_keys=False)


def load_config(path: Optional[Path] = None) -> Config:
    """
    Load configuration from YAML file.

    Args:
        path: Path to config file. If None, returns defaults.

    Returns:
        Config instance with loaded values merged over defaults.
    """
    config = Config()

    if path is None or not path.exists():
        return config

    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return config

    # Apply loaded values
    if "session_name" in data:
        config.session_name = data["session_name"]
    if "port" in data:
        config.port = int(data["port"])
    if "host" in data:
        config.host = data["host"]
    if "theme" in data:
        config.theme = data["theme"]
    if "font_size" in data:
        config.font_size = int(data["font_size"])
    if "scrollback" in data:
        config.scrollback = int(data["scrollback"])
    if "auto_setup" in data:
        config.auto_setup = bool(data["auto_setup"])

    # Quick commands
    if "quick_commands" in data:
        config.quick_commands = [
            QuickCommand(label=cmd["label"], send=cmd["send"])
            for cmd in data["quick_commands"]
        ]

    # Role prefixes
    if "role_prefixes" in data:
        config.role_prefixes = [
            RolePrefix(label=rp["label"], insert=rp["insert"])
            for rp in data["role_prefixes"]
        ]

    # Context buttons
    if "context_buttons" in data:
        config.context_buttons = [
            ContextButton(
                label=cb["label"],
                template=cb["template"],
                prompt_fields=cb.get("prompt_fields", []),
            )
            for cb in data["context_buttons"]
        ]

    # Device overrides
    if "devices" in data:
        for hostname, dev in data["devices"].items():
            config.devices[hostname] = DeviceConfig(
                font_size=int(dev["font_size"]) if dev.get("font_size") else None,
                physical_kb=bool(dev.get("physical_kb", False)),
            )

    # Repos
    if "repos" in data:
        config.repos = []
        for r in data["repos"]:
            # Parse startup_delay_ms with clamping (0..5000)
            delay = int(r.get("startup_delay_ms", 300))
            delay = max(0, min(5000, delay))

            config.repos.append(Repo(
                label=r["label"],
                path=r["path"],
                session=r["session"],
                startup_command=r.get("startup_command"),
                startup_delay_ms=delay,
            ))

    return config


def merge_configs(base: Config, override: Config) -> Config:
    """Merge two configs, with override taking precedence."""
    result = Config(
        session_name=override.session_name if override.session_name != "mobile-term" else base.session_name,
        port=override.port if override.port != 8765 else base.port,
        host=override.host if override.host != "0.0.0.0" else base.host,
        token=override.token or base.token,
        quick_commands=override.quick_commands if override.quick_commands else base.quick_commands,
        role_prefixes=override.role_prefixes if override.role_prefixes else base.role_prefixes,
        context_buttons=override.context_buttons if override.context_buttons else base.context_buttons,
        repos=override.repos if override.repos else base.repos,
        theme=override.theme if override.theme != "dark" else base.theme,
        font_size=override.font_size if override.font_size != 16 else base.font_size,
        scrollback=override.scrollback if override.scrollback != 20000 else base.scrollback,
        project_root=override.project_root or base.project_root,
    )
    return result
