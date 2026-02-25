"""
Agent driver registry.

Usage:
    from mobile_terminal.drivers import get_driver
    driver = get_driver("claude")
    obs = driver.observe(ctx)
"""

from typing import Optional

from .base import AgentDriver, BaseAgentDriver, Observation, ObserveContext
from .claude import ClaudeDriver, ClaudePermissionDetector
from .codex import CodexDriver
from .gemini import GeminiDriver
from .generic import GenericDriver

__all__ = [
    "AgentDriver",
    "BaseAgentDriver",
    "Observation",
    "ObserveContext",
    "ClaudeDriver",
    "ClaudePermissionDetector",
    "CodexDriver",
    "GeminiDriver",
    "GenericDriver",
    "get_driver",
    "register_driver",
]

# Built-in driver registry
_REGISTRY: dict[str, type[BaseAgentDriver]] = {
    "claude": ClaudeDriver,
    "codex": CodexDriver,
    "gemini": GeminiDriver,
    "generic": GenericDriver,
}


def register_driver(agent_type: str, driver_class: type[BaseAgentDriver]) -> None:
    """Register a custom driver class."""
    _REGISTRY[agent_type] = driver_class


def get_driver(agent_type: str, display_name: Optional[str] = None) -> BaseAgentDriver:
    """Get a driver instance by agent_type. Falls back to GenericDriver.

    Args:
        agent_type: Driver identifier (e.g. "claude", "codex", "generic").
        display_name: Optional override for display name.
    """
    cls = _REGISTRY.get(agent_type, GenericDriver)
    driver = cls()
    if display_name:
        driver._display_name = display_name
    return driver
