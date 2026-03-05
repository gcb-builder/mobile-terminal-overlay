"""Router package for Mobile Terminal Overlay.

Each router module exports a ``register(app, deps)`` function that adds
routes to the FastAPI *app* while closing over shared helpers via *deps*.
"""
from dataclasses import dataclass
from typing import Callable


@dataclass
class AppDeps:
    """Shared inner functions created inside ``create_app()`` and passed
    to every router's ``register()`` call."""
    verify_token: Callable
    send_typed: Callable
    get_current_repo_path: Callable
    get_repo_path_info: Callable
    validate_target: Callable
    read_claude_file: Callable
    build_observe_context: Callable
    get_git_head: Callable
    get_git_info_cached: Callable
    try_auto_snapshot: Callable
