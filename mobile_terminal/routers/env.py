"""Routes for .env file management (CRUD for environment variables)."""
import logging
import os
import re
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
MAX_VALUE_LEN = 4096
MAX_FILE_SIZE = 64 * 1024  # 64KB


def parse_env_file(content: str) -> list[dict]:
    """Parse .env content into entries preserving comments and blanks.

    Each entry is one of:
      {"type": "kv", "key": ..., "value": ..., "has_export": bool, "raw": ...}
      {"type": "comment", "raw": ...}
      {"type": "blank", "raw": ""}
    """
    entries = []
    for line in content.splitlines():
        raw = line
        stripped = line.strip()

        if not stripped:
            entries.append({"type": "blank", "raw": ""})
            continue

        if stripped.startswith("#"):
            entries.append({"type": "comment", "raw": raw})
            continue

        # Try to parse KEY=VALUE, optionally prefixed with 'export '
        work = stripped
        has_export = False
        if work.startswith("export "):
            has_export = True
            work = work[7:].lstrip()

        eq_pos = work.find("=")
        if eq_pos < 1:
            # Not a valid kv line, treat as comment
            entries.append({"type": "comment", "raw": raw})
            continue

        key = work[:eq_pos].strip()
        value_part = work[eq_pos + 1:]

        # Strip surrounding quotes from value
        value = value_part.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        entries.append({
            "type": "kv",
            "key": key,
            "value": value,
            "has_export": has_export,
            "raw": raw,
        })

    return entries


def serialize_env_entries(entries: list[dict]) -> str:
    """Reconstruct .env file from entries."""
    lines = []
    for entry in entries:
        if entry["type"] in ("comment", "blank"):
            lines.append(entry["raw"])
            continue

        key = entry["key"]
        value = entry["value"]
        prefix = "export " if entry.get("has_export") else ""

        # Quote values that contain spaces, quotes, #, or special chars
        needs_quoting = any(c in value for c in ' \t"\'#$\\`') or not value
        if needs_quoting:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{prefix}{key}="{escaped}"')
        else:
            lines.append(f"{prefix}{key}={value}")

    return "\n".join(lines) + "\n" if lines else ""


def _resolve_env_path(scope: str, deps) -> Path:
    """Resolve .env file path based on scope."""
    if scope == "repo":
        repo_path = deps.get_current_repo_path()
        if repo_path:
            return repo_path / ".env"
        return Path.cwd() / ".env"
    else:  # server
        return Path.cwd() / ".env"


def _atomic_write_env(path: Path, content: str):
    """Write .env atomically: write to .tmp, fsync, rename. Keep .env.bak."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create backup if original exists
    if path.exists():
        bak = path.with_suffix(".env.bak")
        try:
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    # Write to temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".env.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def register(app: FastAPI, deps):
    """Register env file routes."""

    @app.get("/api/env")
    async def list_env_vars(
        scope: str = Query("repo"),
        _auth=Depends(deps.verify_token),
    ):
        """List environment variables from .env file."""
        if scope not in ("repo", "server"):
            return JSONResponse({"error": "scope must be 'repo' or 'server'"}, status_code=400)

        path = _resolve_env_path(scope, deps)

        if not path.exists():
            return {
                "vars": [],
                "path": str(path),
                "exists": False,
                "scope": scope,
            }

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        if len(content) > MAX_FILE_SIZE:
            return JSONResponse({"error": "File too large (max 64KB)"}, status_code=413)

        entries = parse_env_file(content)
        kv_entries = [
            {"key": e["key"], "value": e["value"]}
            for e in entries if e["type"] == "kv"
        ]

        return {
            "vars": kv_entries,
            "path": str(path),
            "exists": True,
            "scope": scope,
        }

    @app.post("/api/env")
    async def set_env_var(
        request: Request,
        _auth=Depends(deps.verify_token),
    ):
        """Set (upsert) an environment variable in .env file."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        scope = body.get("scope", "repo")
        key = (body.get("key") or "").strip()
        value = body.get("value", "")

        if scope not in ("repo", "server"):
            return JSONResponse({"error": "scope must be 'repo' or 'server'"}, status_code=400)

        if not key or not ENV_KEY_RE.match(key):
            return JSONResponse(
                {"error": f"Invalid key: must match {ENV_KEY_RE.pattern}"},
                status_code=400,
            )

        if not isinstance(value, str):
            return JSONResponse({"error": "Value must be a string"}, status_code=400)

        if len(value) > MAX_VALUE_LEN:
            return JSONResponse(
                {"error": f"Value too long (max {MAX_VALUE_LEN} chars)"},
                status_code=400,
            )

        path = _resolve_env_path(scope, deps)

        # Read existing file or start empty
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as e:
                return JSONResponse({"error": str(e)}, status_code=500)

            if len(content) > MAX_FILE_SIZE:
                return JSONResponse({"error": "File too large (max 64KB)"}, status_code=413)

            entries = parse_env_file(content)
        else:
            entries = []

        # Upsert: find existing key or append
        updated = False
        for entry in entries:
            if entry["type"] == "kv" and entry["key"] == key:
                entry["value"] = value
                updated = True
                break

        if not updated:
            entries.append({
                "type": "kv",
                "key": key,
                "value": value,
                "has_export": False,
                "raw": "",
            })

        try:
            _atomic_write_env(path, serialize_env_entries(entries))
        except Exception as e:
            logger.error(f"Failed to write .env: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        action = "updated" if updated else "added"
        logger.info(f"Env var {action}: {key} (scope={scope})")
        app.state.audit_log.log("env_set", {"key": key, "scope": scope, "updated": updated})

        return {"success": True, "key": key, "updated": updated}

    @app.delete("/api/env/{key}")
    async def delete_env_var(
        key: str,
        scope: str = Query("repo"),
        _auth=Depends(deps.verify_token),
    ):
        """Remove an environment variable from .env file."""
        if scope not in ("repo", "server"):
            return JSONResponse({"error": "scope must be 'repo' or 'server'"}, status_code=400)

        if not ENV_KEY_RE.match(key):
            return JSONResponse({"error": "Invalid key"}, status_code=400)

        path = _resolve_env_path(scope, deps)

        if not path.exists():
            return JSONResponse({"error": ".env file not found"}, status_code=404)

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        entries = parse_env_file(content)

        # Find and remove the key
        found = False
        new_entries = []
        for entry in entries:
            if entry["type"] == "kv" and entry["key"] == key:
                found = True
                continue
            new_entries.append(entry)

        if not found:
            return JSONResponse({"error": f"Key '{key}' not found"}, status_code=404)

        try:
            _atomic_write_env(path, serialize_env_entries(new_entries))
        except Exception as e:
            logger.error(f"Failed to write .env: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        logger.info(f"Env var removed: {key} (scope={scope})")
        app.state.audit_log.log("env_delete", {"key": key, "scope": scope})

        return {"success": True, "key": key}
