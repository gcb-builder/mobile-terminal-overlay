"""Routes for AI challenge / code review."""
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from mobile_terminal.challenge import get_available_models, DEFAULT_MODEL
from mobile_terminal.helpers import get_tmux_target, run_subprocess

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register challenge routes."""

    @app.get("/api/challenge/models")
    async def get_challenge_models(_auth=Depends(deps.verify_token)):
        """
        Get list of available AI models for challenge function.

        Returns only models whose provider has a valid API key configured.
        """

        models = get_available_models()
        return {"models": models, "default": DEFAULT_MODEL}

    @app.post("/api/challenge")
    async def challenge_code(
        _auth=Depends(deps.verify_token),
        model: str = Query(DEFAULT_MODEL),
        problem: str = Query(""),
        include_terminal: bool = Query(True),
        terminal_lines: int = Query(50),
        include_diff: bool = Query(True),
        plan_filename: Optional[str] = Query(None, description="Specific plan filename to include"),
    ):
        """
        Run problem-focused code review using AI models.

        Supports multiple providers: Together.ai, OpenAI, Anthropic.
        User selects model, system routes to appropriate provider.

        Context bundle is built from:
        - User's problem description (required for focused review)
        - Terminal output (optional, captures current state)
        - Git diff (optional, shows recent changes)
        - Active plan (optional, shows what Claude is working on)
        - Minimal project context

        Returns AI's analysis focused on the specific problem.
        """

        repo_path = deps.get_current_repo_path()
        if not repo_path:
            return JSONResponse(
                {"error": "No repo path found"},
                status_code=400,
            )

        # Build problem-focused bundle
        bundle_parts = []

        # 1. Problem statement (user-provided)
        if problem.strip():
            bundle_parts.append(f"## Problem Statement\n{problem.strip()}")
        else:
            bundle_parts.append("## Problem Statement\nGeneral code review requested (no specific problem described)")

        # 2. Terminal content (captures current debugging state)
        if include_terminal:
            session = app.state.current_session
            if session:
                # Use active target pane if set, otherwise fall back to session default
                target = get_tmux_target(session, app.state.active_target)
                try:
                    result = await run_subprocess(
                        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{terminal_lines}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        bundle_parts.append(f"## Current Terminal State (last {terminal_lines} lines)\n```\n{result.stdout}\n```")
                except Exception as e:
                    logger.warning(f"Failed to capture terminal: {e}")

        # 3. Git diff (shows recent changes)
        if include_diff:
            try:
                # Get diff stat first
                diff_stat = await run_subprocess(
                    ["git", "diff", "--stat"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # Get actual diff (limited)
                diff_content = await run_subprocess(
                    ["git", "diff"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if diff_stat.returncode == 0 and diff_stat.stdout.strip():
                    diff_text = diff_content.stdout[:8000] if diff_content.stdout else ""
                    if len(diff_content.stdout) > 8000:
                        diff_text += "\n... [diff truncated]"
                    bundle_parts.append(f"## Uncommitted Changes\n```\n{diff_stat.stdout}\n```\n\n### Diff Detail\n```diff\n{diff_text}\n```")
            except Exception as e:
                logger.warning(f"Failed to get git diff: {e}")

        # 4. Plan (if specified by filename)
        if plan_filename:
            try:
                if not re.match(r'^[\w\-\.]+\.md$', plan_filename):
                    logger.warning(f"Invalid plan filename rejected: {plan_filename}")
                    plan_filename = None
            except Exception:
                plan_filename = None
        if plan_filename:
            try:
                plans_dir = Path.home() / ".claude" / "plans"
                plan_path = plans_dir / plan_filename
                if plan_path.exists():
                    plan_content = plan_path.read_text(errors="replace")
                    # Extract title from first line
                    plan_title = plan_content.split('\n')[0].lstrip('#').strip() or plan_filename
                    if len(plan_content) > 4000:
                        plan_content = plan_content[:4000] + "\n... [plan truncated]"
                    bundle_parts.append(f"## Plan: {plan_title}\n```markdown\n{plan_content}\n```")
            except Exception as e:
                logger.warning(f"Failed to read plan {plan_filename}: {e}")

        # 5. Minimal project context (git status + branch)
        try:
            status = await run_subprocess(
                ["git", "status", "-sb"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if status.returncode == 0:
                bundle_parts.append(f"## Git Status\n```\n{status.stdout}\n```")
        except Exception:
            pass

        bundle = "\n\n".join(bundle_parts)

        # Call API with problem-focused system prompt
        from mobile_terminal.challenge import MODELS
        if model not in MODELS:
            return JSONResponse({"error": f"Unknown model: {model}"}, status_code=400)

        # Adversarial challenge prompt - stress-tests implementation against stated problem
        system_prompt = """## Role

You are an adversarial code reviewer and design challenger. Your job is to stress-test the current implementation and plan against the stated problem.

You are not a collaborator and not a planner.
Assume the implementation may be wrong.

---

## Scope & Focus Rules (Strict)

Focus only on the issue described in Problem Statement.

Use only the provided inputs:
- Terminal output
- Git diff / status
- Plan (if present)

Do not:
- Give generic project feedback
- Suggest running commands
- Re-explain the code unless needed to justify a concern
- Propose large refactors unless the current approach is fundamentally flawed

---

## Evaluation Mandate

Answer four questions:

1. Does the current code actually solve the stated problem?
2. If it appears to work, what assumptions could break it?
3. If it doesn't work, what is the minimal correction?
4. What failure mode is most likely to show up in production first?

---

## Output Format (Enforced)

**Problem Analysis**
Concise restatement of the problem in your own words.
If the problem statement is ambiguous or underspecified, say so explicitly.

**Evidence from Current State**
Concrete observations from:
- terminal output (line-level if relevant)
- git diff (file + behavior level)

**Potential Failure Points**
List specific, testable risks, not hypotheticals.
Example: race condition, stale state, incorrect boundary, missing guard.

**Minimal Corrective Action**
One of:
- "No change required"
- "Small fix" (≤ 3 focused changes)
- "Design mismatch" (current plan does not meet requirements)

**Risks / Edge Cases**
Only the top 1–3 risks worth caring about.

---

## Tone & Constraints

- Be direct, not polite
- Prefer negative certainty ("this will fail if…") over hedging
- If everything looks correct, say so—but still identify one thing to watch"""

        # Build request manually to use custom system prompt
        model_info = MODELS[model]
        provider_key = model_info["provider"]
        model_id = model_info["model_id"]

        from mobile_terminal.challenge import (
            PROVIDERS, get_api_key, validate_api_key,
            build_openai_payload, build_anthropic_payload,
            build_openai_headers, build_anthropic_headers,
            parse_openai_response, parse_anthropic_response,
            parse_openai_responses_response,
        )
        import httpx

        provider = PROVIDERS[provider_key]
        api_key = get_api_key(provider_key)
        is_valid, error_msg = validate_api_key(api_key, provider_key)
        if not is_valid:
            return JSONResponse({"error": error_msg}, status_code=400)

        # Build payload with custom system prompt
        fmt = provider["format"]
        if fmt == "openai":
            headers = build_openai_headers(api_key)
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": bundle},
                ],
                "temperature": 0.2,
                "max_tokens": 1000,
            }
            parse_response = parse_openai_response
        elif fmt == "openai_responses":
            # GPT-5.2+ uses Responses API
            headers = build_openai_headers(api_key)
            payload = {
                "model": model_id,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": bundle},
                ],
                "temperature": 0.2,
                "max_output_tokens": 1000,
            }
            parse_response = parse_openai_responses_response
        elif fmt == "anthropic":
            headers = build_anthropic_headers(api_key)
            payload = {
                "model": model_id,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": bundle},
                ],
                "temperature": 0.2,
                "max_tokens": 1000,
            }
            parse_response = parse_anthropic_response
        else:
            return JSONResponse({"error": f"Unknown format: {fmt}"}, status_code=400)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    provider["url"],
                    headers=headers,
                    json=payload,
                )
                if response.status_code != 200:
                    return JSONResponse(
                        {"error": f"API error {response.status_code}: {response.text}"},
                        status_code=500,
                    )
                data = response.json()
                content = parse_response(data)
                result = {
                    "success": True,
                    "content": content,
                    "model": model,
                    "model_name": model_info["name"],
                    "provider": provider_key,
                    "bundle_chars": len(bundle),
                }
        except httpx.TimeoutException:
            return JSONResponse({"error": "Request timed out (120s)"}, status_code=500)
        except Exception as e:
            logger.error(f"Challenge API error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        if result.get("success"):
            return {
                "success": True,
                "content": result["content"],
                "model": result.get("model"),
                "model_name": result.get("model_name"),
                "provider": result.get("provider"),
                "bundle_chars": result.get("bundle_chars"),
                "usage": result.get("usage", {}),
            }
        else:
            return JSONResponse(
                {"error": result.get("error", "Unknown error")},
                status_code=500,
            )
