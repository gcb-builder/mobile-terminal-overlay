"""Pure helper functions with zero reliance on app or app.state.

Every function here is safe to import at module level without triggering
FastAPI or server-side state initialization.
"""

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ANSI / text helpers
# ---------------------------------------------------------------------------

# ANSI escape sequence pattern for stripping terminal formatting
# Covers: CSI sequences, OSC sequences, character set selection, and other escapes
_ANSI_ESCAPE_RE = re.compile(
    r'\x1b\[[0-9;]*[A-Za-z]'  # CSI sequences (colors, cursor, etc.)
    r'|\x1b\][^\x07]*\x07'     # OSC sequences (title, etc.)
    r'|\x1b[PX^_][^\x1b]*\x1b\\\\'  # DCS/SOS/PM/APC sequences
    r'|\x1b[\(\)][A-Z0-9]'     # Character set selection (e.g., \x1b(B)
    r'|\x1b[=>]'               # Keypad modes
    r'|\x1b[78]'               # Save/restore cursor
    r'|\x1b[DME]'              # Line operations
)


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text for plain tail output."""
    return _ANSI_ESCAPE_RE.sub('', text)


def get_project_id(repo_path: Path, strip_leading: bool = False) -> str:
    """Convert repo path to Claude project ID string.

    Matches the directory naming convention used by Claude Code under
    ~/.claude/projects/.
    """
    pid = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
    return pid.lstrip("-") if strip_leading else pid


def find_utf8_boundary(data: bytes, max_len: int) -> int:
    """Find the last valid UTF-8 character boundary at or before max_len.

    Avoids splitting multi-byte UTF-8 characters which causes garbled output.
    Returns the safe cut position (may be less than max_len).
    """
    if max_len >= len(data):
        return len(data)

    # Start at max_len and scan backwards for a valid boundary
    pos = max_len

    # UTF-8 continuation bytes have pattern 10xxxxxx (0x80-0xBF)
    # We need to find a byte that is NOT a continuation byte
    while pos > 0 and pos > max_len - 4:  # UTF-8 chars are at most 4 bytes
        byte = data[pos]
        # Check if this is a continuation byte (10xxxxxx)
        if (byte & 0xC0) != 0x80:
            # This is either ASCII (0xxxxxxx) or a start byte (11xxxxxx)
            # Safe to cut here
            return pos
        pos -= 1

    # Fallback: couldn't find boundary, use max_len (rare edge case)
    return max_len


# ---------------------------------------------------------------------------
# Async subprocess wrapper
# ---------------------------------------------------------------------------

async def run_subprocess(*args, **kwargs):
    """Async wrapper for subprocess.run — runs in thread pool to avoid blocking event loop.

    Accepts the same arguments as subprocess.run. Defaults to capture_output=True,
    text=True, timeout=5 if not specified.
    """
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    kwargs.setdefault('timeout', 5)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(*args, **kwargs)
    )


# ---------------------------------------------------------------------------
# tmux target helpers
# ---------------------------------------------------------------------------

def get_tmux_target(session_name: str, active_target: str) -> str:
    """
    Convert active_target to tmux target format.

    active_target is stored as "window:pane" (e.g., "2:0")
    tmux expects "session:window.pane" (e.g., "claude:2.0")

    Returns session_name if active_target is None or invalid.
    """
    if not active_target:
        return session_name
    parts = active_target.split(":")
    if len(parts) == 2:
        return f"{session_name}:{parts[0]}.{parts[1]}"
    return session_name


async def get_bounded_snapshot(session: str, active_target: str = None, max_bytes: int = 16000) -> str:
    """Get bounded tmux capture-pane snapshot for mode switch catchup.

    Returns screen content with ANSI (-e) for accurate rendering.
    Auto-reduces line count if output exceeds max_bytes.
    """
    target = get_tmux_target(session, active_target) if active_target else session

    # Start with 50 lines, reduce if too large
    content = ""
    for lines in [50, 30, 20, 10]:
        try:
            result = await run_subprocess(
                ["tmux", "capture-pane", "-p", "-e", "-S", f"-{lines}", "-t", target],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                content = result.stdout or ""
                if len(content) <= max_bytes:
                    return content
                # Too large, try fewer lines
                continue
            else:
                return ""
        except Exception:
            return ""

    # Fallback: return whatever we got, truncated
    return content[:max_bytes] if content else ""


# ---------------------------------------------------------------------------
# Capture-pane cache
# ---------------------------------------------------------------------------

# Key: (session, pane_id, lines), Value: (timestamp, result)
_capture_cache: dict = {}
CAPTURE_CACHE_TTL = 0.3  # 300ms TTL


def get_cached_capture(session: str, pane_id: str, lines: int) -> Optional[dict]:
    """Get cached capture-pane result if still valid."""
    key = (session, pane_id, lines)
    if key in _capture_cache:
        ts, result = _capture_cache[key]
        if time.time() - ts < CAPTURE_CACHE_TTL:
            return result
    return None


def set_cached_capture(session: str, pane_id: str, lines: int, result: dict):
    """Cache capture-pane result."""
    key = (session, pane_id, lines)
    _capture_cache[key] = (time.time(), result)
    # Clean old entries (keep cache small)
    now = time.time()
    stale = [k for k, (ts, _) in _capture_cache.items() if now - ts > CAPTURE_CACHE_TTL * 10]
    for k in stale:
        del _capture_cache[k]


# ---------------------------------------------------------------------------
# Log API cache
# ---------------------------------------------------------------------------

# Key: (project_id, pane_id), Value: (timestamp, file_mtime, result)
_log_cache: dict = {}
LOG_CACHE_TTL = 2.0  # 2 second TTL - log content changes slowly


def get_cached_log(project_id: str, pane_id: Optional[str], file_mtime: float) -> Optional[dict]:
    """Get cached log result if still valid and file hasn't changed."""
    key = (project_id, pane_id or "")
    if key in _log_cache:
        ts, cached_mtime, result = _log_cache[key]
        # Valid if within TTL AND file hasn't been modified
        if time.time() - ts < LOG_CACHE_TTL and cached_mtime == file_mtime:
            return result
    return None


def set_cached_log(project_id: str, pane_id: Optional[str], file_mtime: float, result: dict):
    """Cache log result."""
    key = (project_id, pane_id or "")
    _log_cache[key] = (time.time(), file_mtime, result)
    # Clean old entries
    now = time.time()
    stale = [k for k, (ts, _, _) in _log_cache.items() if now - ts > LOG_CACHE_TTL * 10]
    for k in stale:
        del _log_cache[k]


# ---------------------------------------------------------------------------
# Tool output cache (for on-demand expand)
# ---------------------------------------------------------------------------

# LRU cache: keyed on (log_path, mtime, tool_use_id)
# Value: dict with content, is_error, line_count, char_count, truncated
_tool_output_cache: dict = {}
_tool_output_order: list = []  # Track insertion order for LRU eviction
TOOL_OUTPUT_CACHE_MAX = 100
TOOL_OUTPUT_CACHE_TTL = 60.0  # seconds
TOOL_OUTPUT_MAX_CHARS = 100_000  # 100KB truncation limit


def get_cached_tool_output(log_path: str, mtime: float, tool_use_id: str) -> Optional[dict]:
    """Get cached tool output if still valid."""
    key = (log_path, mtime, tool_use_id)
    if key in _tool_output_cache:
        ts, result = _tool_output_cache[key]
        if time.time() - ts < TOOL_OUTPUT_CACHE_TTL:
            return result
        # Expired
        del _tool_output_cache[key]
        if key in _tool_output_order:
            _tool_output_order.remove(key)
    return None


def set_cached_tool_output(log_path: str, mtime: float, tool_use_id: str, result: dict):
    """Cache tool output with LRU eviction."""
    key = (log_path, mtime, tool_use_id)
    _tool_output_cache[key] = (time.time(), result)
    if key in _tool_output_order:
        _tool_output_order.remove(key)
    _tool_output_order.append(key)
    # Evict oldest if over limit
    while len(_tool_output_order) > TOOL_OUTPUT_CACHE_MAX:
        old_key = _tool_output_order.pop(0)
        _tool_output_cache.pop(old_key, None)


# ---------------------------------------------------------------------------
# Plan links
# ---------------------------------------------------------------------------

PLAN_LINKS_FILE = Path.home() / ".claude" / "plan-links.json"


def get_plan_links() -> dict:
    """Read plan-links.json, return empty dict if not found."""
    if PLAN_LINKS_FILE.exists():
        try:
            return json.loads(PLAN_LINKS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_plan_links(links: dict):
    """Write plan-links.json."""
    PLAN_LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAN_LINKS_FILE.write_text(json.dumps(links, indent=2))


def score_plan_for_repo(plan_path: Path, repo_path: Path) -> int:
    """
    Score how well a plan matches a repo based on content.
    Higher score = better match.
    """
    try:
        text = plan_path.read_text(errors="replace")
    except Exception:
        return 0

    repo_str = str(repo_path)
    repo_name = repo_path.name
    parent_str = str(repo_path.parent)

    score = 0
    if repo_str in text:
        score += 3  # Full path match
    if repo_name in text:
        score += 2  # Repo name match
    if parent_str in text:
        score += 1  # Parent path match

    return score


def get_plans_for_repo(repo_path: Path) -> list:
    """
    Get plans matching a repo, sorted by score then modification time.
    Returns list of (plan_path, score) tuples.
    """
    plans_dir = Path.home() / ".claude" / "plans"
    if not plans_dir.exists():
        return []

    scored = []
    for plan in plans_dir.glob("*.md"):
        score = score_plan_for_repo(plan, repo_path)
        if score > 0:
            scored.append((plan, score))

    # Sort by score desc, then mtime desc
    scored.sort(key=lambda x: (x[1], x[0].stat().st_mtime), reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Claude Code settings (user-global)
# ---------------------------------------------------------------------------

CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
MCP_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')
MCP_MAX_ARGS_SIZE = 4096

PLUGIN_NAME_RE = re.compile(r'^[a-zA-Z0-9._@/-]{1,128}$')
INSTALLED_PLUGINS_FILE = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def load_claude_settings() -> tuple:
    """Read ~/.claude/settings.json. Returns (dict, error_or_None).
    If file doesn't exist, returns ({}, None).
    If file is invalid JSON, returns ({}, error_message) —
    callers MUST NOT write when error is set (would destroy user data)."""
    try:
        return json.loads(CLAUDE_SETTINGS_FILE.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return {}, None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Invalid settings.json: {e}")
        return {}, f"settings.json is invalid JSON: {e}"


def save_claude_settings(settings: dict):
    """Atomic write: .tmp + fsync + rename. Keeps one .bak."""
    path = CLAUDE_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    bak = path.with_suffix(".json.bak")
    data = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        shutil.copy2(path, bak)
    except FileNotFoundError:
        pass  # No existing file to back up
    tmp.rename(path)


# ---------------------------------------------------------------------------
# tmux session / window management (sync, no app dependency)
# ---------------------------------------------------------------------------

def list_tmux_sessions(prefix: str = "") -> list:
    """List tmux sessions, optionally filtered by prefix."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        sessions = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        if prefix:
            sessions = [s for s in sessions if s.startswith(prefix)]
        return sessions
    except Exception as e:
        logger.error(f"Error listing tmux sessions: {e}")
        return []


def _tmux_session_exists(session: str) -> bool:
    """Check if a tmux session exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _list_session_windows(session: str) -> list:
    """List windows in a tmux session with their pane info.

    Returns list of dicts with window_index, window_name, pane_id, cwd.
    Only returns the first pane per window.
    """
    try:
        result = subprocess.run(
            [
                "tmux", "list-panes", "-s", "-t", session,
                "-F", "#{window_index}|#{window_name}|#{pane_id}|#{pane_current_path}"
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []

        windows = []
        seen_indices = set()
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split("|", 3)
            if len(parts) < 4:
                continue
            win_idx = parts[0]
            # Only take first pane per window
            if win_idx in seen_indices:
                continue
            seen_indices.add(win_idx)
            windows.append({
                "window_index": win_idx,
                "window_name": parts[1],
                "pane_id": parts[2],
                "cwd": parts[3],
            })
        return windows
    except Exception as e:
        logger.error(f"auto_setup: error listing session windows: {e}")
        return []


def _match_repo_to_window(repo, windows: list) -> Optional[dict]:
    """Match a repo to an existing tmux window using three-pass strategy.

    1. Exact name match (case-insensitive)
    2. Prefix match (handles suffixed names like 'geo-cv-a3f2')
    3. cwd match (resolved paths)
    """
    repo_label_lower = repo.label.lower()

    # Pass 1: exact name match
    for w in windows:
        if w["window_name"].lower() == repo_label_lower:
            return w

    # Pass 2: prefix match (window name starts with repo label)
    for w in windows:
        if w["window_name"].lower().startswith(repo_label_lower):
            return w

    # Pass 3: cwd match
    try:
        repo_resolved = str(Path(repo.path).resolve())
    except Exception:
        return None
    for w in windows:
        try:
            if str(Path(w["cwd"]).resolve()) == repo_resolved:
                return w
        except Exception:
            continue

    return None


def _get_pane_command(pane_id: str) -> Optional[str]:
    """Get the foreground command running in a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        return None
    except Exception:
        return None


def _create_tmux_window(session: str, window_name: str, path: str) -> dict:
    """Create a new tmux window in a session.

    Returns dict with target_id and pane_id.
    Raises RuntimeError on failure.
    """
    result = subprocess.run(
        [
            "tmux", "new-window",
            "-t", f"{session}:",
            "-n", window_name,
            "-c", path,
            "-P", "-F", "#{window_index}:#{pane_index}|#{pane_id}"
        ],
        capture_output=True, text=True, timeout=10,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Failed to create window")

    output = result.stdout.strip()
    if "|" not in output:
        raise RuntimeError(f"Unexpected tmux output format: '{output}'")

    parts = output.split("|")
    return {
        "target_id": parts[0],
        "pane_id": parts[1] if len(parts) > 1 else None,
    }


async def _send_startup_command(pane_id: str, command: str, delay_seconds: float = 0.3):
    """Send a startup command to a tmux pane after a delay.

    Automatically unsets CLAUDECODE env var before the command to prevent
    "nested session" errors when launching Claude Code from a server that
    was itself started inside a Claude Code session.
    """
    await asyncio.sleep(delay_seconds)
    try:
        # Clear CLAUDECODE so agent CLIs don't refuse to start
        actual_cmd = f"unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; {command}"
        await run_subprocess(
            ["tmux", "send-keys", "-t", pane_id, "-l", actual_cmd],
            capture_output=True, timeout=5,
        )
        await run_subprocess(
            ["tmux", "send-keys", "-t", pane_id, "Enter"],
            capture_output=True, timeout=5,
        )
        logger.info(f"auto_setup: sent startup command '{command}' to pane {pane_id}")
    except Exception as e:
        logger.error(f"auto_setup: failed to send startup command to {pane_id}: {e}")


async def ensure_tmux_setup(config) -> dict:
    """Create or adopt a tmux session, ensuring all configured repos have windows.

    Returns a summary dict with session status, adopted/created windows, and any errors.
    """
    session = config.session_name
    result = {
        "session": session,
        "created_session": False,
        "adopted_windows": [],
        "created_windows": [],
        "skipped_commands": [],
        "errors": [],
    }

    # Filter repos belonging to this session
    repos = [r for r in config.repos if r.session == session]
    if not repos:
        logger.info(f"auto_setup: no repos configured for session '{session}', skipping")
        return result

    handled_repos = set()

    # Check if session exists, create if not
    if not _tmux_session_exists(session):
        first_repo = repos[0]
        first_path = str(Path(first_repo.path).resolve())

        if not Path(first_repo.path).exists():
            msg = f"auto_setup: first repo path does not exist: {first_repo.path}"
            logger.error(msg)
            result["errors"].append(msg)
            return result

        try:
            # Sanitize window name
            win_name = re.sub(r'[^a-zA-Z0-9_.-]', '', first_repo.label)[:50] or "window"
            proc = await run_subprocess(
                ["tmux", "new-session", "-d", "-s", session, "-n", win_name, "-c", first_path],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                msg = f"auto_setup: failed to create session: {proc.stderr.strip()}"
                logger.error(msg)
                result["errors"].append(msg)
                return result

            result["created_session"] = True
            handled_repos.add(first_repo.label)
            result["created_windows"].append(first_repo.label)
            logger.info(f"auto_setup: created session '{session}' with window '{win_name}'")

            # Send startup command for the newly created first window
            if first_repo.startup_command:
                # Get pane ID of the first window
                windows = _list_session_windows(session)
                if windows:
                    pane_id = windows[0]["pane_id"]
                    delay = first_repo.startup_delay_ms / 1000.0
                    asyncio.create_task(
                        _send_startup_command(pane_id, first_repo.startup_command, delay)
                    )
                    logger.info(f"auto_setup: queued startup command for '{first_repo.label}'")

        except Exception as e:
            msg = f"auto_setup: exception creating session: {e}"
            logger.error(msg)
            result["errors"].append(msg)
            return result
    else:
        logger.info(f"auto_setup: session '{session}' already exists, adopting")

    # Scrub agent env vars from both the tmux global and session environments
    # so that new windows/panes get a clean shell (prevents "nested session"
    # errors when the server itself was started inside Claude Code).
    # Session-level -u only removes the session override, so we must also
    # scrub the global environment where tmux inherited these vars.
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        await run_subprocess(
            ["tmux", "set-environment", "-g", "-u", var],
            capture_output=True, timeout=5,
        )
        await run_subprocess(
            ["tmux", "set-environment", "-t", session, "-u", var],
            capture_output=True, timeout=5,
        )

    # List existing windows and match remaining repos
    windows = _list_session_windows(session)

    for repo in repos:
        if repo.label in handled_repos:
            continue

        matched = _match_repo_to_window(repo, windows)
        if matched:
            result["adopted_windows"].append(repo.label)
            logger.info(
                f"auto_setup: adopted window '{matched['window_name']}' "
                f"(index {matched['window_index']}) for repo '{repo.label}'"
            )
            # Never send startup commands to adopted windows
            if repo.startup_command:
                result["skipped_commands"].append(repo.label)
        else:
            # Create a new window for this repo
            repo_path = Path(repo.path)
            if not repo_path.exists():
                msg = f"auto_setup: repo path does not exist: {repo.path}"
                logger.warning(msg)
                result["errors"].append(msg)
                continue

            try:
                win_name = re.sub(r'[^a-zA-Z0-9_.-]', '', repo.label)[:50] or "window"
                win_info = _create_tmux_window(session, win_name, str(repo_path.resolve()))
                result["created_windows"].append(repo.label)
                logger.info(f"auto_setup: created window '{win_name}' for repo '{repo.label}'")

                # Send startup command only for newly created windows with a shell
                if repo.startup_command and win_info.get("pane_id"):
                    pane_cmd = _get_pane_command(win_info["pane_id"])
                    shell_names = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
                    if pane_cmd and pane_cmd.lower() in shell_names:
                        delay = repo.startup_delay_ms / 1000.0
                        asyncio.create_task(
                            _send_startup_command(win_info["pane_id"], repo.startup_command, delay)
                        )
                        logger.info(f"auto_setup: queued startup command for '{repo.label}'")
                    else:
                        result["skipped_commands"].append(repo.label)
                        logger.info(
                            f"auto_setup: skipped startup command for '{repo.label}' "
                            f"(pane running '{pane_cmd}')"
                        )
            except RuntimeError as e:
                msg = f"auto_setup: failed to create window for '{repo.label}': {e}"
                logger.error(msg)
                result["errors"].append(msg)

    summary = (
        f"auto_setup: session='{session}' created={result['created_session']} "
        f"adopted={len(result['adopted_windows'])} created={len(result['created_windows'])} "
        f"errors={len(result['errors'])}"
    )
    logger.info(summary)

    return result


# ---------------------------------------------------------------------------
# Process management helpers
# ---------------------------------------------------------------------------

def _sigchld_handler(signum, frame):
    """Reap zombie child processes."""
    try:
        while True:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
            logger.debug(f"Reaped child process {pid} with status {status}")
    except ChildProcessError:
        pass  # No child processes


# Install SIGCHLD handler to prevent zombie processes
signal.signal(signal.SIGCHLD, _sigchld_handler)


def _resolve_device(request, devices: dict):
    """Resolve client IP to a Tailscale hostname and return matching DeviceConfig."""
    if not devices:
        return None
    client_ip = request.client.host if request.client else None
    if not client_ip:
        return None
    try:
        result = subprocess.run(
            ["tailscale", "whois", "--json", client_ip],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            hostname = info.get("Node", {}).get("ComputedName", "")
            if hostname in devices:
                return devices[hostname]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# PTY helpers
# ---------------------------------------------------------------------------

def set_terminal_size(fd: int, cols: int, rows: int, child_pid: int = None) -> None:
    """
    Set terminal size using TIOCSWINSZ ioctl.

    Args:
        fd: File descriptor of the pty master.
        cols: Number of columns.
        rows: Number of rows.
        child_pid: Optional child process ID to send SIGWINCH for redraw.
    """
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    # Send SIGWINCH to trigger tmux redraw
    if child_pid:
        try:
            os.kill(child_pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Constants re-exported for convenience
# ---------------------------------------------------------------------------

# Directory containing static files
STATIC_DIR = Path(__file__).parent / "static"

# Directory for cached Claude conversation logs (persists across /clear)
LOG_CACHE_DIR = Path.home() / ".cache" / "mobile-overlay" / "logs"
