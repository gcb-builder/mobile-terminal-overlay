"""PermissionDaemon — single-source permission detector (Phase 2).

Goal: collapse the existing 3-detector matrix (terminal_session.py
singleton, push.py scanner, client extractPermissionPrompt) into one
authoritative path. Phase 1 ran the daemon read-only as a shadow
detector to validate correlation. Phase 2 (this file) gives the
daemon authority to fire — it sends "1\\n"/Escape, writes the live
audit log, and emits banners. The push.py scanner remains as a
backstop, but defers to the daemon via a shared dedup set on
app.state.fired_perms (keyed by stable_id, 30s TTL).

Correlation rules (conservative — a missed perm is annoying, a stray
fire is worse):
  - JSONL unresolved tool_use AND visible prompt AND visible body
    substring-matches jsonl target → real perm, stable_id = tool_use_id
  - Visible prompt with PRE-CHECK marker AND no JSONL → pre-check
    pattern, stable_id = hash(normalized visible)
  - JSONL unresolved + no visible prompt → tool executing, skip
  - Visible only without pre-check marker → stale scrollback, skip
  - JSONL+visible but commands don't match → wrong correlation, skip

Shadow log (kept across phases for diagnostics) is JSONL at
~/.cache/mobile-overlay/permission-daemon-shadow.jsonl. Live audit
goes through PermissionPolicy.audit → permission-audit.jsonl.
"""

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SHADOW_LOG = Path.home() / ".cache" / "mobile-overlay" / "permission-daemon-shadow.jsonl"

# Phase 3.5: Claude Code writes ~/.claude/sessions/{pid}.json with
# `status: "waiting"` and `waitingFor: "approve <Tool>"` in real time
# whenever a permission prompt is showing — BEFORE the tool_use is
# committed to JSONL (Claude defers commit on some commands). This
# is undocumented but live and reliable. See structural-rebuild RCA.
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
# v=448: stale-session check is now PID-aliveness via /proc/{pid}, not
# a time cutoff on updatedAt. Claude only refreshes updatedAt on state
# transitions — a prompt sitting waiting for human approval can sit at
# updatedAt > 30s even though the process is alive. Confirmed 2026-04-26
# via secondbrain pane: deploy-web Bash prompt visible 17min, sessions
# file age 75s, daemon never fired because the time cutoff filtered it.

# Pre-check warnings Claude Code shows BEFORE writing the tool_use to JSONL.
# When we see one of these strings in the visible prompt, JSONL will be silent
# during the wait — so we can't correlate against tool_use_id; we use the
# normalized visible content as the dedup hash instead.
#
# Add a new entry whenever a previously-unknown pre-check warning shows
# up in user reports as "no fire". The substring should be distinctive
# enough that no agent-prose false-positive trips it.
PRE_CHECK_MARKERS = (
    "unhandled node type:",
    "contains command_substitution",
    "contains file_redirect",
    "contains process_substitution",
    # 2026-04-25: QR PNG generation hit this — `python -c "..."` with a
    # multi-line script. Distinctive substring of the longer message:
    # "Newline followed by # inside a quoted argument can hide arguments
    # from path validation".
    "hide arguments from path validation",
    # 2026-04-25 (later): heredoc-based psql commands like
    # `PGPASSWORD=... psql ... <<'SQL' ... SQL` triggered Claude's
    # "Command appears to be an incomplete fragment" pre-check. Same
    # 2-option Yes/No pattern, JSONL silent during the wait. Multiple
    # back-to-back user reports of "auto-approval not firing" for
    # heredoc psql; this marker covers the entire family.
    "appears to be an incomplete fragment",
    # 2026-04-25 (later still): cross-project file access prompts.
    # Claude renders a 3-option Bash with option 2 = "Yes, allow
    # reading from <path> from this project" when the command reads
    # from a directory outside the active project. JSONL is silent
    # during the prompt (deferred commit). Distinctive substring on
    # the option label: "allow reading from".
    "allow reading from",
)

# Box-drawing characters Claude wraps prompts in. Stripped before regex
# matching so `│  ❯ 1. Yes  │` matches anchored selector regex.
_BOX_CHARS = re.compile(r"[│╭╮╰╯─┌┐└┘├┤┬┴┼]")
# Permission selector: line starts with optional whitespace, then ❯ or >,
# then space, then digit-period-space. MULTILINE so each line is anchored.
_SELECTOR_RE = re.compile(r"^[ \t]*[❯>][ \t]+\d+\.[ \t]+", re.MULTILINE)
# Question phrases that indicate a permission prompt (case-insensitive).
# These pair with a numbered selector (`❯ 1. Yes` etc.) to confirm the
# capture is a real permission prompt and not agent prose. Claude has
# multiple question wordings depending on the tool — keep this list
# wide enough to catch all of them, narrow enough that agent text
# doesn't accidentally trigger.
_PROMPT_PHRASES = (
    "do you want to proceed?",
    "do you want to make this edit",   # Edit / multi-edit prompts
    "do you want to create",            # Write prompts ("create new file?")
    "do you want to overwrite",         # Write over existing file
    "allow this action?",
    "approve this",
    "permission to",
)


@dataclass
class VisiblePrompt:
    """Parsed permission prompt from terminal capture."""
    question: str           # the "Do you want to proceed?" or similar line
    body: str               # text between tool header and the question
                            # (typically the command/file path being approved)
    full_text: str          # entire normalized capture, for hashing
    has_pre_check_marker: bool


@dataclass
class PermCandidate:
    """A pending permission identified by the daemon."""
    stable_id: str          # tool_use_id when JSONL agrees; content-hash for pre-check
    pane: str               # window:pane string, e.g. "2:0"
    tool: str               # Bash | Edit | Write | Read | ...
    target: str             # command / file_path / pattern
    repo_path: str          # cwd-derived repo, used by policy
    signal: str             # "jsonl_visible_correlated" | "pre_check_visible_only"


@dataclass
class ShadowRecord:
    """One row in the shadow audit log."""
    ts: float
    stable_id: str
    pane: str
    tool: str
    target: str
    signal: str
    decision: str           # allow | deny | prompt
    reason: str
    rule_id: Optional[str]
    risk: str
    would_fire: bool        # True when Phase 2 would inject keys


# 1.5s mtime-style cache for _load_waiting_sessions. Daemon (2s) +
# scanner (3s) + /api/permissions/waiting (per browser scrape) all hit
# this — uncached it ran ~6-10 times/sec, each a 50-file glob+read.
_SESSIONS_CACHE: dict = {"ts": 0.0, "data": {}}
_SESSIONS_CACHE_TTL = 1.5


def _load_waiting_sessions(now: Optional[float] = None, force: bool = False) -> dict:
    """Read ~/.claude/sessions/*.json and return a dict keyed by cwd.

    Each entry: {"sessionId", "pid", "waitingFor", "tool", "updatedAt"}.
    Only sessions where status == "waiting" AND waitingFor starts with
    "approve " AND the PID is still alive (/proc/{pid}) are included —
    protects against stale session files left behind by long-dead
    Claude processes without filtering live but long-pending waits.

    Falls back to empty dict on any IO/parse error, so daemon's other
    correlation paths remain functional if the file format changes.

    Cached for _SESSIONS_CACHE_TTL seconds to amortize the cost across
    daemon ticks + scanner ticks + /api/permissions/waiting requests
    (uncached: ~6-10 calls/sec × 50 file reads each).
    """
    if now is None:
        now = time.time()
    if not force and (now - _SESSIONS_CACHE["ts"]) < _SESSIONS_CACHE_TTL:
        return _SESSIONS_CACHE["data"]

    out: dict = {}
    if not CLAUDE_SESSIONS_DIR.is_dir():
        _SESSIONS_CACHE["ts"] = now
        _SESSIONS_CACHE["data"] = out
        return out
    try:
        files = list(CLAUDE_SESSIONS_DIR.glob("*.json"))
    except Exception:
        return _SESSIONS_CACHE["data"]  # serve last good value
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("status") != "waiting":
            continue
        waiting_for = data.get("waitingFor", "")
        if not waiting_for.startswith("approve "):
            continue
        # PID-aliveness instead of updatedAt age (see SESSION_WAITING_MAX_AGE
        # comment): a long-pending prompt with a live process is exactly
        # the case we want to fire on.
        pid = data.get("pid", 0)
        if not pid or not Path(f"/proc/{pid}").exists():
            continue
        cwd = data.get("cwd", "")
        if not cwd:
            continue
        # waitingFor format: "approve Bash" / "approve Edit" / etc.
        tool = waiting_for[len("approve "):].strip()
        out[cwd] = {
            "sessionId": data.get("sessionId", ""),
            "pid": data.get("pid", 0),
            "waitingFor": waiting_for,
            "tool": tool,
            "updatedAt": data.get("updatedAt", 0),
        }
    _SESSIONS_CACHE["ts"] = now
    _SESSIONS_CACHE["data"] = out
    return out


def _normalize_for_hash(text: str) -> str:
    """Strip box chars + collapse whitespace so re-renders of the same prompt
    (cursor blink, status line ticks) hash to the same id."""
    s = _BOX_CHARS.sub(" ", text)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# Lines containing these patterns are dropped from the capture-content
# cache hash because they tick every second even when the prompt is
# stable, defeating the cache. Spinner glyphs from Claude TUI's status
# bar + token-count + elapsed-time substrings.
#
# CAREFUL: `●` and `◯` are tool/output markers in Claude TUI
# ("● Bash(...)") and MUST NOT be in this set — stripping them would
# remove real agent activity from the cache hash, defeating
# invalidation when the agent runs a new tool.
_SPINNER_LINE_RE = re.compile(
    r"[✻✶✷✢⠂⠃⠄⠆⠇⠋⠙⠹⠸⠼⠴⠦⠧]"           # spinner glyphs (no ● or ◯)
    r"|\(\d+(?:m\s*)?\d*s\b"                # "(36s" / "(1m 12s"
    r"|↓\s*\d+(?:\.\d+)?k?\s+tokens"        # "↓ 750 tokens" / "↓ 12.4k tokens"
    r"|cooked for|brewed for|writing|thinking|architecting|wiring",
    re.IGNORECASE,
)


def _normalize_capture_for_cache(capture: str) -> str:
    """Return capture text with volatile spinner/timer lines removed,
    so the cache hash is stable across ticks where only the spinner
    advances. Non-volatile lines (prompt body, selectors, agent output)
    pass through unchanged."""
    if not capture:
        return ""
    out = []
    for line in capture.split("\n"):
        if _SPINNER_LINE_RE.search(line):
            continue
        out.append(line)
    return "\n".join(out)


def _command_substring_match(visible_body: str, jsonl_target: str) -> bool:
    """Tolerant substring match for correlation. Either the visible body
    contains the JSONL target (typical: visible shows the full command),
    OR the JSONL target's first 200 chars contain the visible body
    (when visible was truncated by terminal width). Either direction
    counts as a real correlation."""
    if not visible_body or not jsonl_target:
        return False
    v = re.sub(r"\s+", " ", visible_body.strip().lower())
    t = re.sub(r"\s+", " ", jsonl_target.strip().lower())
    if not v or not t:
        return False
    return v in t or t[:200] in v or v[:60] in t


def _parse_visible_prompt(capture: str) -> Optional[VisiblePrompt]:
    """Return a VisiblePrompt if the capture contains a permission prompt,
    else None. Conservative — both selector AND a question-line/marker
    must be present (just selector by itself = numbered list in agent prose)."""
    if not capture:
        return None
    stripped = _BOX_CHARS.sub(" ", capture)
    has_selector = _SELECTOR_RE.search(stripped) is not None
    if not has_selector:
        return None

    # Find the LAST selector line (lowest in capture = newest = live).
    # Picking the first would lock onto stale prompts in scrollback when
    # the agent has rendered a fresh prompt below — confirmed bug
    # 2026-04-25: pane had a stale "hide arguments" pre-check above a
    # live PGPASSWORD 3-option Bash, parser locked onto the stale one
    # and computed a precheck stable_id; v=422 dedup then blocked all
    # subsequent ticks, leaving the live prompt waiting forever.
    lines = stripped.split("\n")
    selector_idx = -1
    prev_selector_idx = -1
    for i, l in enumerate(lines):
        if _SELECTOR_RE.match(l):
            prev_selector_idx = selector_idx  # remember previous before overwriting
            selector_idx = i
    if selector_idx < 0:
        return None

    # Marker / question check on lines NEAR THE CHOSEN SELECTOR (between
    # the previous selector and this one, or last 30 lines if first).
    # Without this scope restriction, an OLD pre-check warning sitting
    # in scrollback above the live prompt still flips has_pre_check_marker
    # to True on the LIVE prompt, causing daemon to fire the wrong path.
    region_start = max(prev_selector_idx + 1, selector_idx - 30)
    region_end = min(selector_idx + 5, len(lines))
    region = "\n".join(lines[region_start:region_end])
    # Whitespace-collapse for substring matching across wrapped lines.
    region_low = re.sub(r"\s+", " ", region).lower()
    has_question = any(p in region_low for p in _PROMPT_PHRASES)
    has_marker = any(m in region_low for m in PRE_CHECK_MARKERS)
    if not (has_question or has_marker):
        return None

    question = ""
    body_parts = []
    for i in range(max(0, selector_idx - 12), selector_idx):
        l = lines[i].strip()
        if not l:
            continue
        if l.endswith("?") and not question:
            question = l
        elif not question:
            body_parts.append(l)
    body = " ".join(body_parts).strip()

    return VisiblePrompt(
        question=question,
        body=body,
        full_text=stripped,
        has_pre_check_marker=has_marker,
    )


def _correlate(
    pane: str,
    repo_path: str,
    jsonl_unresolved: Optional[dict],
    visible: Optional[VisiblePrompt],
    session_waiting: Optional[dict] = None,
) -> Optional[PermCandidate]:
    """Apply the correlation rules. See module docstring for the full table."""
    # Case 1: JSONL + visible correlate by command substring match.
    if jsonl_unresolved and visible:
        if _command_substring_match(visible.body, jsonl_unresolved.get("target", "")):
            return PermCandidate(
                stable_id=jsonl_unresolved.get("id", ""),
                pane=pane,
                tool=jsonl_unresolved.get("name", ""),
                target=jsonl_unresolved.get("target", ""),
                repo_path=repo_path,
                signal="jsonl_visible_correlated",
            )
        # 1b: pre-check + JSONL combo. Claude sometimes shows a pre-check
        # warning ("Command appears to be an incomplete fragment", etc.)
        # AND writes the tool_use to JSONL simultaneously — the visible
        # body is the WARNING TEXT (not the command), so the substring
        # match fails. When the marker is present and the JSONL tool is
        # Bash (every known pre-check pattern is Bash), trust JSONL and
        # use the real tool_use_id.
        # Confirmed by production 2026-04-25: PGPASSWORD heredoc psql
        # rendered "Command appears to be an incomplete fragment" with
        # JSONL holding the unresolved Bash tool_use; before this branch
        # daemon skipped → user had to manually tap "1".
        if visible.has_pre_check_marker and jsonl_unresolved.get("name") == "Bash":
            return PermCandidate(
                stable_id=jsonl_unresolved.get("id", ""),
                pane=pane,
                tool="Bash",
                target=jsonl_unresolved.get("target", ""),
                repo_path=repo_path,
                signal="jsonl_visible_correlated_precheck",
            )
        # JSONL says unresolved but visible's command doesn't match the
        # tool_use's target. Without a sessions/ confirmation this is
        # likely a stale visible from a prior operation — skip. With
        # sessions/ saying "waiting", trust it: the visible IS the live
        # prompt, JSONL just hasn't caught up yet.
        if not session_waiting:
            return None
        # else fall through to Case 3

    # Case 3: Claude's own session file (~/.claude/sessions/{pid}.json)
    # reports status="waiting" with waitingFor="approve <Tool>". This is
    # the most authoritative signal — it's set BY CLAUDE the moment the
    # prompt is rendered, BEFORE the tool_use lands in JSONL. Used when
    # marker-based detection (Case 2) fails because Claude shows a new
    # permission category we haven't catalogued. Conservative gates:
    #   - visible MUST have a selector + question (real prompt rendered)
    #   - session.cwd MUST match pane.cwd (no cross-pane bleed)
    #   - session.updatedAt MUST be recent (handled by _load_waiting_sessions)
    # The tool comes from session["tool"] (parsed from waitingFor).
    if visible and session_waiting:
        tool = session_waiting.get("tool", "Bash")
        # stable_id = visible body+question hash (same shape as Case 2)
        # so the existing PRECHECK_REFIRE_TTL dedup applies and we don't
        # re-fire the same prompt for an hour.
        content = _normalize_for_hash(visible.body + "|" + visible.question)
        digest = hashlib.md5(content.encode("utf-8")).hexdigest()
        return PermCandidate(
            stable_id=f"precheck:{digest[:16]}",
            pane=pane,
            tool=tool,
            target=visible.body[:200],
            repo_path=repo_path,
            signal="session_waiting",
        )

    # Case 2: visible only with PRE-CHECK marker. Claude doesn't write the
    # tool_use to JSONL during pre-checks, so JSONL is silent. Tool
    # defaults to Bash — all known pre-checks (file_redirect,
    # command_substitution, hide_arguments) are Bash.
    #
    # P2.5: hash body+question instead of full_text. full_text includes
    # status-bar drift (token counter, elapsed time, spinner glyph) that
    # changes every tick — production showed 18 different precheck:XXX
    # ids in 35s for the same prompt. body and question are parsed
    # deterministically by _parse_visible_prompt and contain only the
    # prompt content.
    #
    # P2.6: set target = body so derive_pane_key produces a per-prompt
    # key. Empty target made every pre-check Bash on the same pane share
    # one pane_key, so the second distinct pre-check within 30s would
    # have been wrongly skipped by fired_perms dedup.
    if visible and visible.has_pre_check_marker and not jsonl_unresolved:
        content = _normalize_for_hash(visible.body + "|" + visible.question)
        digest = hashlib.md5(content.encode("utf-8")).hexdigest()
        return PermCandidate(
            stable_id=f"precheck:{digest[:16]}",
            pane=pane,
            tool="Bash",
            target=visible.body[:200],
            repo_path=repo_path,
            signal="pre_check_visible_only",
        )

    # All other cases: skip. Conservative — missed perm > stray fire.
    return None


FIRED_TTL = 30.0  # shared dedup window across daemon + scanner + /decide.
                  # If a perm was fired within this window, no detector
                  # should fire it again. Keyed by stable_id on
                  # app.state.fired_perms.

# Pre-check stable_ids are content-hashes of body+question. Same hash
# within minutes-to-hours almost always = stale scrollback (Claude
# already answered, the prompt box is just still in the visible buffer)
# OR a paused agent that was idle for a while. The 30s FIRED_TTL is too
# short — we observed re-fires landing in chat input ~31s after the
# original fire. 5 min was also too short — same content kept re-firing
# at the 5-min boundary in production over 11:29-11:35 (multiple
# distinct prompts, each TTL-rollover firing 5 min later). Bumped to
# 1 hour: legitimate retries of the *exact same body+question hash*
# within an hour are rare; almost every recurrence is stale scrollback.
PRECHECK_REFIRE_TTL = 3600.0  # 1 hour


def derive_pane_key(pane: str, tool: str, target: str) -> str:
    """Detector-agnostic dedup key. Daemon knows the JSONL tool_use_id;
    scanner and /decide don't. All three can compute this from
    {pane, tool, target} and use it as a shared lock so daemon-fires
    and scanner-fires-and-/decide-fires don't double-tap the same prompt."""
    h = hashlib.md5(target.encode("utf-8", "replace")).hexdigest()[:12]
    return f"pane:{pane}:{tool}:{h}"


def mark_fired(app, *keys: str) -> None:
    """Stamp one or more dedup keys. Callable from any detector
    (daemon, scanner, /decide). Daemon stamps both stable_id (its own
    primary key) AND the derive_pane_key (cross-detector key) so other
    paths can dedup against either."""
    fired = getattr(app.state, "fired_perms", None)
    if fired is None:
        fired = {}
        app.state.fired_perms = fired
    now = time.time()
    for k in keys:
        if k:
            fired[k] = now


def was_recently_fired(app, *keys: str, ttl: float = FIRED_TTL) -> bool:
    """Check the shared dedup set against any of the given keys.
    Returns True if any was fired within ttl seconds."""
    fired = getattr(app.state, "fired_perms", None)
    if not fired:
        return False
    now = time.time()
    for k in keys:
        if not k:
            continue
        last = fired.get(k, 0)
        if (now - last) < ttl:
            return True
    return False


class PermissionDaemon:
    """Single-source permission detector (Phase 2).

    Runs as a background task. Every POLL_INTERVAL seconds, scans all panes,
    correlates JSONL state with visible terminal text, evaluates policy,
    and (when the action is allow/deny) fires "1\\n" or Escape via tmux
    send-keys. Stamps app.state.fired_perms with the stable_id so the
    push.py scanner backstop and /decide endpoint don't double-fire.

    Acceptance criteria for Phase 2:
      - daemon fires for allow/deny decisions
      - daemon stamps fired_perms before sending keys
      - same stable_id never fires twice within FIRED_TTL
      - banner still emits for needs_human cases
      - shadow log still populated (diagnostic continuity from Phase 1)
    """

    POLL_INTERVAL = 2.0     # tradeoff: 1s = closer to scanner cadence, 2s = lighter
    SEEN_TTL = 60.0         # shadow-log dedup (avoid shadow noise on repeated scans)

    def __init__(self, app, deps):
        self.app = app
        self.deps = deps
        self._task: Optional[asyncio.Task] = None
        self._seen_ids: dict[str, float] = {}
        self._fire_lock = asyncio.Lock()  # serializes fires across panes —
                                           # avoids two simultaneous "1\n"
                                           # injections racing on a slow tmux
        # v=436: per-pane cache of (capture_hash, eval_result). When the
        # pane capture is byte-identical to the last evaluation, skip
        # parse + JSONL read + correlate and return the cached result.
        # Production showed ~47 redundant parses/min from stale scrollback
        # content that hadn't changed between ticks.
        self._eval_cache: dict[str, tuple] = {}  # target_id -> (capture_md5, result)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="permission_daemon")
        logger.info("[perm_daemon] started — live firing mode (Phase 2)")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            logger.info("[perm_daemon] stopped")

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[perm_daemon] tick error: {e}", exc_info=True)

    async def _tick(self) -> None:
        session = getattr(self.app.state, "current_session", None)
        if not session:
            return
        loop = asyncio.get_event_loop()
        try:
            panes = await loop.run_in_executor(None, self._list_panes_sync, session)
        except Exception as e:
            logger.debug(f"[perm_daemon] list_panes failed: {e}")
            return
        for target_id, pane_cwd, tmux_t in panes:
            await self.evaluate_and_fire(session, target_id, pane_cwd, tmux_t)

    async def evaluate_and_fire(
        self, session: str, target_id: str, pane_cwd: str, tmux_t: str
    ) -> bool:
        """Evaluate ONE pane and fire if policy allows. Public entry
        point used by both the daemon's own tick AND the push.py scanner
        backstop. Owns: parsing, correlation, policy evaluation, dedup,
        race-protected staged Enter, audit, banner broadcast.

        Returns True iff the pane had a perm that was processed (fired
        OR deliberately skipped via dedup/race-protect), False if
        nothing to do for this pane."""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._evaluate_pane_sync,
                session, target_id, pane_cwd, tmux_t,
            )
            if result is None:
                return False
            perm, decision, req = result
            await self._handle_decision(perm, decision, req, tmux_t)
            return True
        except Exception as e:
            logger.debug(f"[perm_daemon] eval pane {target_id} failed: {e}", exc_info=True)
            return False

        # Sweep stale seen entries
        now = time.time()
        if len(self._seen_ids) > 200:
            self._seen_ids = {k: v for k, v in self._seen_ids.items() if now - v < self.SEEN_TTL}
        # Sweep shared fired_perms too — keep app.state from growing forever.
        # Window must exceed PRECHECK_REFIRE_TTL so the long-window stale-
        # scrollback dedup still finds entries when it checks.
        fired = getattr(self.app.state, "fired_perms", None)
        if fired and len(fired) > 200:
            sweep_keep_ttl = max(FIRED_TTL * 4, PRECHECK_REFIRE_TTL + 60)
            self.app.state.fired_perms = {
                k: v for k, v in fired.items() if now - v < sweep_keep_ttl
            }

    def _list_panes_sync(self, session: str) -> list[tuple[str, str, str]]:
        from mobile_terminal.helpers import get_tmux_target
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", session, "-F",
             "#{window_index}:#{pane_index}|#{pane_current_path}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        out = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) < 2:
                continue
            target_id = parts[0]
            pane_cwd = parts[1]
            out.append((target_id, pane_cwd, get_tmux_target(session, target_id)))
        return out

    def _evaluate_pane_sync(self, session: str, target_id: str,
                             pane_cwd: str, tmux_t: str):
        """Sync detection: capture pane, parse JSONL, correlate, evaluate
        policy. Returns (PermCandidate, PermissionDecision) when there's a
        live perm to act on, else None. Writes the shadow record as a
        side effect (diagnostic continuity from Phase 1).

        Pure of side effects on tmux — firing happens in _handle_decision
        on the event loop."""
        # Capture pane (-S -50 to catch the question even when status indicators
        # push it up; matches scanner's window).
        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", tmux_t, "-S", "-50"],
                capture_output=True, text=True, timeout=2,
            )
            capture_text = cap.stdout if cap.returncode == 0 else ""
        except Exception:
            return None

        # v=436: capture-content cache. If the capture bytes are identical
        # to the previous evaluation for this pane, the parse + JSONL +
        # correlate result is also identical — return the cached result
        # without re-doing the work. Production: ~47 stale-scrollback
        # detections/minute were all re-parsing identical content.
        #
        # v=437: hash a NORMALIZED capture that strips spinner/timer
        # lines (`✻ Brewed for 36s`, `· ↓ 750 tokens`, etc.) — without
        # this normalization the spinner counter changed every tick and
        # the cache missed on every call (only got 13% effective).
        cap_hash = hashlib.md5(
            _normalize_capture_for_cache(capture_text).encode("utf-8", "replace")
        ).hexdigest()
        cached = self._eval_cache.get(target_id)
        if cached is not None and cached[0] == cap_hash:
            return cached[1]

        # v=434: Claude TUI has a "queue messages while busy" mode.
        # While the agent is executing a tool, typed input becomes a
        # QUEUED MESSAGE shown as `❯ <text>` above a `❯ Press up to
        # edit queued messages` line. If we send "1" in this mode it
        # lands in the queue — Backspace doesn't reach the queue, so
        # the "1" eventually submits when the agent finishes (showing
        # up as a stray "1" chat message). Confirmed bug 2026-04-25.
        # Pre-fire guard: refuse to fire if the pane is in queue mode.
        if "press up to edit queued messages" in capture_text.lower():
            return None

        visible = _parse_visible_prompt(capture_text)

        # Find or seed JSONL detector for this pane
        from mobile_terminal.drivers.claude import _find_unresolved_tool_use
        detect_fn = getattr(self.app.state, "_detect_target_log_file", None)
        log_file: Optional[Path] = None
        if detect_fn:
            try:
                from mobile_terminal.helpers import get_project_id
                claude_dir = Path.home() / ".claude" / "projects" / get_project_id(Path(pane_cwd))
                if claude_dir.exists():
                    log_file = detect_fn(target_id, session, claude_dir)
            except Exception:
                log_file = None

        jsonl_unresolved: Optional[dict] = None
        if log_file:
            try:
                jsonl_unresolved = _find_unresolved_tool_use(log_file)
            except Exception:
                jsonl_unresolved = None

        # Phase 3.5: Claude's session file is the most authoritative
        # signal — set BEFORE JSONL commit. Cheap to read (small file,
        # ~50 of them in the dir). Filtered to status=waiting + recent.
        try:
            waiting_sessions = _load_waiting_sessions()
            session_waiting = waiting_sessions.get(pane_cwd)
        except Exception:
            session_waiting = None

        perm = _correlate(target_id, pane_cwd, jsonl_unresolved, visible, session_waiting)
        if not perm:
            self._eval_cache[target_id] = (cap_hash, None)
            return None

        # Run policy
        from mobile_terminal.permission_policy import normalize_request
        policy = self.app.state.permission_policy
        req = normalize_request(
            {"tool": perm.tool, "target": perm.target, "id": perm.stable_id},
            perm.repo_path,
        )
        decision = policy.evaluate(req)

        # Shadow log — collapsed per stable_id within SEEN_TTL so the log
        # stays readable. Distinct from app.state.fired_perms (which gates
        # actual firing); this is purely diagnostic.
        now = time.time()
        last_shadow = self._seen_ids.get(perm.stable_id, 0)
        if now - last_shadow >= self.SEEN_TTL:
            self._seen_ids[perm.stable_id] = now
            record = ShadowRecord(
                ts=now,
                stable_id=perm.stable_id,
                pane=perm.pane,
                tool=perm.tool,
                target=perm.target[:200],
                signal=perm.signal,
                decision=decision.action,
                reason=decision.reason,
                rule_id=decision.rule_id,
                risk=decision.risk,
                would_fire=decision.action in ("allow", "deny"),
            )
            self._write_shadow(record)

        result = (perm, decision, req)
        self._eval_cache[target_id] = (cap_hash, result)
        return result

    async def _handle_decision(self, perm, decision, req, tmux_t: str) -> None:
        """Async branch of the tick: fire keys for allow/deny, emit
        banner for needs_human. Cross-detector dedup via fired_perms —
        if scanner or /decide already fired this prompt under any key
        (stable_id or derived pane key), daemon must skip, otherwise we
        get the v=409 "1 1 1 1" cascade.

        Pre-check stable_ids get an extra long-window check: same content
        hash within PRECHECK_REFIRE_TTL = stale scrollback (we saw a
        confirmed bug where the same precheck:8cbd80b re-fired exactly
        31s after the first fire and landed in chat input)."""
        pane_key = derive_pane_key(perm.pane, perm.tool, perm.target)
        if was_recently_fired(self.app, perm.stable_id, pane_key):
            return
        if perm.signal == "pre_check_visible_only" and was_recently_fired(
            self.app, perm.stable_id, ttl=PRECHECK_REFIRE_TTL
        ):
            # DEBUG (was INFO): on busy panes the same precheck stable_id
            # gets re-detected ~40 times/min (each diagnostic Bash adds
            # new content → cache miss → full parse → finds the OLD
            # selector still in scrollback → this dedup correctly skips).
            # The behavior is right; the log was just noise.
            logger.debug(
                f"[perm_daemon] skip — precheck {perm.stable_id[:24]} "
                f"already fired within {PRECHECK_REFIRE_TTL:.0f}s "
                f"(stale scrollback prevention)"
            )
            return
        if decision.action in ("allow", "deny"):
            await self._fire_perm(perm, decision, req, tmux_t, pane_key)
        elif decision.action == "prompt":
            await self._emit_banner(perm, decision, req)

    async def _fire_perm(self, perm, decision, req, tmux_t: str, pane_key: str) -> None:
        """Send "1\\n" (allow) or Escape (deny), write audit, emit
        permission_auto banner. Stamps fired_perms (both stable_id AND
        pane_key) BEFORE sending keys so a concurrent scanner/decide
        can't double-fire."""
        async with self._fire_lock:
            # Re-check inside the lock — another tick could have fired
            # while we were waiting on the lock.
            if was_recently_fired(self.app, perm.stable_id, pane_key):
                return

            # v=435: pre-fire sanity, ALL inside the lock + immediately
            # before send_keys. Two checks consolidated:
            #
            # (1) Queue-mode: Claude TUI shows "Press up to edit queued
            #     messages" when agent is busy and incoming keys are
            #     queued instead of consumed. Sending "1" here lands in
            #     a queue Backspace can't reach → eventually submits as
            #     a chat message. Refuse to fire.
            #
            # (2) Sessions/ status: if the prompt was already resolved
            #     (user pressed 1 manually, or Claude completed past it)
            #     between _evaluate and now, the "1" lands in chat input.
            #     v=431 + v=434 cover most of this; this check tightens
            #     the window from ~50-500ms to <5ms.
            #
            # Stamp fired_perms on either skip so we don't re-evaluate
            # the same perm immediately on the next tick.
            try:
                cap = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", tmux_t, "-S", "-30"],
                    capture_output=True, text=True, timeout=2,
                )
                fresh_capture = cap.stdout if cap.returncode == 0 else ""
            except Exception:
                fresh_capture = ""
            if "press up to edit queued messages" in fresh_capture.lower():
                logger.info(
                    f"[perm_daemon] skip fire — queue mode at send time "
                    f"({perm.signal} {perm.stable_id[:16]} pane={perm.pane})"
                )
                mark_fired(self.app, perm.stable_id, pane_key)
                return
            if perm.signal in ("session_waiting", "jsonl_visible_correlated_precheck"):
                try:
                    fresh = _load_waiting_sessions(force=True)
                    if perm.repo_path not in fresh:
                        logger.info(
                            f"[perm_daemon] skip fire — session no longer waiting "
                            f"at send time ({perm.signal} {perm.stable_id[:16]} "
                            f"pane={perm.pane})"
                        )
                        mark_fired(self.app, perm.stable_id, pane_key)
                        return
                except Exception:
                    pass

            mark_fired(self.app, perm.stable_id, pane_key)

            runtime = self.app.state.runtime
            try:
                if decision.action == "allow":
                    # Staged fire to defeat the chat-input race:
                    #   1) send "1"
                    #   2) wait 60ms for Claude TUI to consume it
                    #   3) re-check sessions/{pid}.json status
                    #      - if no longer waiting → "1" was consumed by the
                    #        prompt OR landed in chat input AND user already
                    #        moved past. DON'T send Enter (would submit chat
                    #        input as a "1" message — confirmed bug 2026-04-25
                    #        where pane 2:0's Claude read "1" as "continue"
                    #        and started committing PA.6).
                    #      - if still waiting → "1" hasn't been consumed,
                    #        send Enter to commit the selection.
                    await runtime.send_keys(tmux_t, "1", literal=True)
                    await asyncio.sleep(0.06)
                    try:
                        # force=True: bypass the 1.5s cache so the recheck
                        # actually reflects post-keypress state.
                        post = _load_waiting_sessions(force=True)
                        still_waiting = perm.repo_path in post
                    except Exception:
                        still_waiting = True  # default: send Enter (current behavior)
                    if still_waiting:
                        await runtime.send_keys(tmux_t, "Enter")
                    else:
                        # "1" was consumed by Claude's prompt (status flipped to
                        # busy) OR landed in chat input. Either way, send a
                        # Backspace to clean up: if Claude consumed it, BSpace
                        # has nothing to do (chat input is empty); if it
                        # landed in chat input, BSpace deletes the orphan "1".
                        # Tradeoff: if user typed concurrently, one user
                        # keystroke gets backspaced. Rare in practice; better
                        # than accumulating orphan "1"s that prefix the
                        # user's next message.
                        try:
                            await runtime.send_keys(tmux_t, "BSpace")
                        except Exception:
                            pass
                        logger.info(
                            f"[perm_daemon] skip Enter — status changed mid-fire "
                            f"({perm.signal} {perm.stable_id[:16]} pane={perm.pane}); "
                            f"sent Backspace to clear orphan \"1\""
                        )
                else:  # deny
                    # Escape cancels every prompt variant; "n" doesn't always map.
                    await runtime.send_keys(tmux_t, "Escape")
            except Exception as e:
                logger.warning(
                    f"[perm_daemon] send_keys failed for {perm.stable_id[:16]}: {e}"
                )
                return

            # Live audit (same writer the scanner + /decide use, so the
            # audit log stays unified across firing paths).
            try:
                self.app.state.permission_policy.audit(req, decision)
            except Exception as e:
                logger.debug(f"[perm_daemon] audit write failed: {e}")

            logger.info(
                f"[perm_daemon] {decision.action.upper()} {perm.tool} "
                f"in {perm.pane} (id={perm.stable_id[:16]}): "
                f"{decision.reason}"
            )

            # permission_auto banner — same shape the scanner emits, so the
            # frontend treats both sources identically. Broadcasts to all
            # connected sinks (multi-sink ready).
            try:
                from pathlib import Path as _P
                from mobile_terminal.transport import broadcast_typed
                repo_name = _P(perm.repo_path).name if perm.repo_path else ""
                await broadcast_typed(
                    self.app, "permission_auto",
                    {
                        "decision": decision.action,
                        "tool": req.tool,
                        "target": req.target,
                        "reason": decision.reason,
                        "pane": perm.pane,
                        "repo": repo_name,
                    },
                    level="info",
                )
            except Exception as e:
                logger.debug(f"[perm_daemon] permission_auto emit failed: {e}")

    async def _emit_banner(self, perm, decision, req) -> None:
        """Emit permission_request banner for needs_human cases. Stamps
        fired_perms with a short-lived 'banner:<id>' key so we don't
        re-banner the same prompt every 2s."""
        banner_key = f"banner:{perm.stable_id}"
        if was_recently_fired(self.app, banner_key):
            return
        mark_fired(self.app, banner_key)

        try:
            from mobile_terminal.transport import broadcast_typed
            banner_perm = {
                "id": perm.stable_id,
                "tool": req.tool,
                "target": req.target,
                "context": "",
                "repo": perm.repo_path or "",
                "risk": req.risk,
                "source_pane": perm.pane,
            }
            await broadcast_typed(
                self.app, "permission_request", banner_perm, level="urgent",
            )
            logger.info(
                f"[perm_daemon] needs_human banner emitted: "
                f"{perm.tool} in {perm.pane} ({decision.reason})"
            )
        except Exception as e:
            logger.debug(f"[perm_daemon] banner emit failed: {e}")

    def _write_shadow(self, record: ShadowRecord) -> None:
        try:
            SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(SHADOW_LOG, "a") as f:
                f.write(json.dumps(record.__dict__) + "\n")
            logger.info(
                f"[perm_daemon] shadow: stable_id={record.stable_id[:16]} "
                f"pane={record.pane} tool={record.tool} signal={record.signal} "
                f"decision={record.decision}/{record.reason} "
                f"would_fire={record.would_fire}"
            )
        except Exception as e:
            logger.debug(f"[perm_daemon] shadow write failed: {e}")
