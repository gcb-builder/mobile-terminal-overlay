"""
Challenge function - skeptical code review using DeepSeek via Together.ai API.
"""

import os
import subprocess
import httpx
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Together.ai API configuration
TOGETHER_API_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_MODEL = "deepseek-ai/DeepSeek-V3"
MAX_BUNDLE_CHARS = 20000
MAX_TOKENS = 500
TEMPERATURE = 0.2

SYSTEM_PROMPT = """You are a strict skeptical code reviewer. Do not suggest running commands.
Do not propose code edits. Be concise. Output format:

Risks:
Missing checks/tests:
Clarifying questions (1-3):"""


def get_together_api_key() -> Optional[str]:
    """Get Together.ai API key from environment."""
    return os.environ.get("TOGETHER_API_KEY")


def validate_api_key(api_key: Optional[str]) -> tuple[bool, str]:
    """
    Validate Together.ai API key format.

    Returns:
        (is_valid, error_message)
    """
    if not api_key:
        return False, "TOGETHER_API_KEY environment variable not set"

    api_key = api_key.strip()

    if len(api_key) < 20:
        return False, "API key appears too short"

    if " " in api_key or "\n" in api_key:
        return False, "API key contains invalid characters (spaces/newlines)"

    return True, ""


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


async def call_together_api(bundle: str) -> dict:
    """
    Call Together.ai API with the challenge bundle.

    Returns:
        dict with 'success', 'content' or 'error' keys
    """
    api_key = get_together_api_key()

    # Validate API key
    is_valid, error_msg = validate_api_key(api_key)
    if not is_valid:
        return {
            "success": False,
            "error": error_msg,
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": TOGETHER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Review this project state and provide your skeptical analysis:\n\n{bundle}"},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                TOGETHER_API_URL,
                headers=headers,
                json=payload,
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"API error {response.status_code}: {response.text}",
                }

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            return {
                "success": True,
                "content": content,
                "model": TOGETHER_MODEL,
                "usage": data.get("usage", {}),
            }

    except httpx.TimeoutException:
        return {
            "success": False,
            "error": "Request timed out (60s)",
        }
    except Exception as e:
        logger.error(f"Together API error: {e}")
        return {
            "success": False,
            "error": str(e),
        }


async def run_challenge(repo_path: Path, log_content: str = "") -> dict:
    """
    Run the full challenge function.

    1. Build context bundle
    2. Call Together.ai API
    3. Return result
    """
    # Build the bundle
    bundle = build_challenge_bundle(repo_path, log_content)

    # Call the API
    result = await call_together_api(bundle)

    # Add bundle info to result
    result["bundle_chars"] = len(bundle)

    return result
