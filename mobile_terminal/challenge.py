"""
Challenge function - skeptical code review using multiple AI providers.

Supports: Together.ai, OpenAI, Anthropic
User selects model, system routes to appropriate provider.
"""

import os
import subprocess
import httpx
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ============================================================================
# Provider Configuration
# ============================================================================

PROVIDERS = {
    "together": {
        "name": "Together.ai",
        "url": "https://api.together.xyz/v1/chat/completions",
        "env_key": "TOGETHER_API_KEY",
        "format": "openai",
    },
    "openai": {
        "name": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "env_key": "OPENAI_API_KEY",
        "format": "openai",
    },
    "anthropic": {
        "name": "Anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "env_key": "ANTHROPIC_API_KEY",
        "format": "anthropic",
    },
}

# Model list - user-facing keys map to provider + model_id
MODELS = {
    "deepseek-v3": {
        "name": "DeepSeek V3",
        "provider": "together",
        "model_id": "deepseek-ai/DeepSeek-V3",
    },
    "deepseek-r1": {
        "name": "DeepSeek R1 (reasoning)",
        "provider": "together",
        "model_id": "deepseek-ai/DeepSeek-R1",
    },
    "qwen-coder": {
        "name": "Qwen 2.5 Coder 32B",
        "provider": "together",
        "model_id": "Qwen/Qwen2.5-Coder-32B-Instruct",
    },
    "llama-70b": {
        "name": "Llama 3.3 70B",
        "provider": "together",
        "model_id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    "gpt-4o-mini": {
        "name": "GPT-4o Mini",
        "provider": "openai",
        "model_id": "gpt-4o-mini",
    },
    "gpt-4o": {
        "name": "GPT-4o",
        "provider": "openai",
        "model_id": "gpt-4o",
    },
    "claude-sonnet": {
        "name": "Claude 3.5 Sonnet",
        "provider": "anthropic",
        "model_id": "claude-3-5-sonnet-20241022",
    },
}

DEFAULT_MODEL = "deepseek-v3"

# ============================================================================
# Shared Configuration
# ============================================================================

MAX_BUNDLE_CHARS = 20000
MAX_TOKENS = 500
TEMPERATURE = 0.2

SYSTEM_PROMPT = """You are a strict skeptical code reviewer. Do not suggest running commands.
Do not propose code edits. Be concise. Output format:

Risks:
Missing checks/tests:
Clarifying questions (1-3):"""


# ============================================================================
# API Key Helpers
# ============================================================================

def get_api_key(provider: str) -> Optional[str]:
    """Get API key for a provider from environment."""
    if provider not in PROVIDERS:
        return None
    env_key = PROVIDERS[provider]["env_key"]
    return os.environ.get(env_key)


def validate_api_key(api_key: Optional[str], provider: str = "together") -> tuple[bool, str]:
    """
    Validate API key format.

    Returns:
        (is_valid, error_message)
    """
    if not api_key:
        env_key = PROVIDERS.get(provider, {}).get("env_key", "API_KEY")
        return False, f"{env_key} environment variable not set"

    api_key = api_key.strip()

    if len(api_key) < 20:
        return False, "API key appears too short"

    if " " in api_key or "\n" in api_key:
        return False, "API key contains invalid characters (spaces/newlines)"

    return True, ""


def get_available_models() -> list[dict]:
    """
    Get list of models that have valid API keys configured.

    Returns:
        List of {key, name} dicts for available models
    """
    available = []
    for model_key, model_info in MODELS.items():
        provider = model_info["provider"]
        api_key = get_api_key(provider)
        if api_key:
            is_valid, _ = validate_api_key(api_key, provider)
            if is_valid:
                available.append({
                    "key": model_key,
                    "name": model_info["name"],
                })
    return available


# ============================================================================
# Bundle Builder
# ============================================================================

def build_challenge_bundle(repo_path: Path, log_content: str = "") -> str:
    """
    Build a context bundle for the challenge function.

    Contents:
    1. Repo name + timestamp
    2. git status -sb
    3. Current branch name
    4. .claude/CONTEXT.md (truncated if needed)
    5. .claude/touch-summary.md (truncated if needed)
    6. Recent log content (truncated if needed)
    """
    import datetime

    bundle_parts = []

    # 1. Repo name + timestamp
    repo_name = repo_path.name
    timestamp = datetime.datetime.now().isoformat()
    bundle_parts.append(f"# Repository: {repo_name}")
    bundle_parts.append(f"# Timestamp: {timestamp}")
    bundle_parts.append("")

    # 2. Git status
    try:
        result = subprocess.run(
            ["git", "status", "-sb"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            bundle_parts.append("## Git Status")
            bundle_parts.append("```")
            bundle_parts.append(result.stdout.strip())
            bundle_parts.append("```")
            bundle_parts.append("")
    except Exception as e:
        logger.warning(f"Failed to get git status: {e}")

    # 3. Current branch
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            bundle_parts.append(f"## Branch: {branch}")
            bundle_parts.append("")
    except Exception as e:
        logger.warning(f"Failed to get branch: {e}")

    # Calculate remaining space
    current_len = len("\n".join(bundle_parts))
    remaining = MAX_BUNDLE_CHARS - current_len

    # 4. CONTEXT.md (allocate ~40% of remaining)
    context_file = repo_path / ".claude" / "CONTEXT.md"
    if context_file.exists():
        try:
            content = context_file.read_text(errors="replace")
            max_context = int(remaining * 0.4)
            if len(content) > max_context:
                content = content[:max_context] + "\n... [truncated]"
            bundle_parts.append("## CONTEXT.md")
            bundle_parts.append(content)
            bundle_parts.append("")
        except Exception as e:
            logger.warning(f"Failed to read CONTEXT.md: {e}")

    # 5. touch-summary.md (allocate ~30% of remaining)
    touch_file = repo_path / ".claude" / "touch-summary.md"
    if touch_file.exists():
        try:
            content = touch_file.read_text(errors="replace")
            max_touch = int(remaining * 0.3)
            if len(content) > max_touch:
                content = content[:max_touch] + "\n... [truncated]"
            bundle_parts.append("## touch-summary.md")
            bundle_parts.append(content)
            bundle_parts.append("")
        except Exception as e:
            logger.warning(f"Failed to read touch-summary.md: {e}")

    # 6. Log content (use remaining space)
    if log_content:
        current_len = len("\n".join(bundle_parts))
        max_log = MAX_BUNDLE_CHARS - current_len - 100  # Leave some margin
        if len(log_content) > max_log:
            log_content = log_content[-max_log:] + "\n... [truncated from start]"
        bundle_parts.append("## Recent Activity")
        bundle_parts.append(log_content)

    return "\n".join(bundle_parts)


# ============================================================================
# API Format Handlers
# ============================================================================

def build_openai_payload(model_id: str, bundle: str) -> dict:
    """Build payload for OpenAI-compatible APIs (OpenAI, Together.ai)."""
    return {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Review this project state and provide your skeptical analysis:\n\n{bundle}"},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }


def build_anthropic_payload(model_id: str, bundle: str) -> dict:
    """Build payload for Anthropic API."""
    return {
        "model": model_id,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": f"Review this project state and provide your skeptical analysis:\n\n{bundle}"},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }


def build_openai_headers(api_key: str) -> dict:
    """Build headers for OpenAI-compatible APIs."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def build_anthropic_headers(api_key: str) -> dict:
    """Build headers for Anthropic API."""
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }


def parse_openai_response(data: dict) -> str:
    """Parse content from OpenAI-compatible response."""
    return data["choices"][0]["message"]["content"]


def parse_anthropic_response(data: dict) -> str:
    """Parse content from Anthropic response."""
    return data["content"][0]["text"]


# ============================================================================
# Main API Call
# ============================================================================

async def call_api(model_key: str, bundle: str) -> dict:
    """
    Call AI API with the challenge bundle.

    Args:
        model_key: Key from MODELS dict (e.g., "deepseek-v3", "gpt-4o-mini")
        bundle: The context bundle to send

    Returns:
        dict with 'success', 'content' or 'error' keys
    """
    # Validate model key
    if model_key not in MODELS:
        return {
            "success": False,
            "error": f"Unknown model: {model_key}",
        }

    model_info = MODELS[model_key]
    provider_key = model_info["provider"]
    model_id = model_info["model_id"]

    # Validate provider
    if provider_key not in PROVIDERS:
        return {
            "success": False,
            "error": f"Unknown provider: {provider_key}",
        }

    provider = PROVIDERS[provider_key]
    api_key = get_api_key(provider_key)

    # Validate API key
    is_valid, error_msg = validate_api_key(api_key, provider_key)
    if not is_valid:
        return {
            "success": False,
            "error": error_msg,
        }

    # Build request based on provider format
    fmt = provider["format"]
    if fmt == "openai":
        headers = build_openai_headers(api_key)
        payload = build_openai_payload(model_id, bundle)
        parse_response = parse_openai_response
    elif fmt == "anthropic":
        headers = build_anthropic_headers(api_key)
        payload = build_anthropic_payload(model_id, bundle)
        parse_response = parse_anthropic_response
    else:
        return {
            "success": False,
            "error": f"Unknown format: {fmt}",
        }

    # Make API call
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                provider["url"],
                headers=headers,
                json=payload,
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"API error {response.status_code}: {response.text}",
                }

            data = response.json()
            content = parse_response(data)

            return {
                "success": True,
                "content": content,
                "model": model_key,
                "model_name": model_info["name"],
                "provider": provider_key,
                "usage": data.get("usage", {}),
            }

    except httpx.TimeoutException:
        return {
            "success": False,
            "error": "Request timed out (120s)",
        }
    except Exception as e:
        logger.error(f"API error ({provider_key}): {e}")
        return {
            "success": False,
            "error": str(e),
        }


# ============================================================================
# Main Entry Point
# ============================================================================

async def run_challenge(repo_path: Path, log_content: str = "", model_key: str = DEFAULT_MODEL) -> dict:
    """
    Run the full challenge function.

    Args:
        repo_path: Path to the repository
        log_content: Optional log content to include
        model_key: Model to use (default: deepseek-v3)

    Returns:
        dict with result
    """
    # Build the bundle
    bundle = build_challenge_bundle(repo_path, log_content)

    # Call the API
    result = await call_api(model_key, bundle)

    # Add bundle info to result
    result["bundle_chars"] = len(bundle)

    return result


# ============================================================================
# Legacy compatibility
# ============================================================================

def get_together_api_key() -> Optional[str]:
    """Legacy: Get Together.ai API key from environment."""
    return get_api_key("together")
