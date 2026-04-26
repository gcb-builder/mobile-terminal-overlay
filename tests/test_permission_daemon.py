"""Tests for permission_daemon's pure helpers — correlation, parsing,
stable_id derivation, and the cross-detector fired_perms dedup.
The daemon's tick loop (subprocess + filesystem) is exercised separately
by deploying and inspecting the shadow log; these tests pin the
safety-critical correlation + dedup logic.

Safety contract: a missed permission is annoying, a stray fire is worse.
Every "skip" case below would, if dropped, cause the live scanner to
inject "1\\n" into the wrong pane.
"""

import re
import time
import pytest

from mobile_terminal.permission_daemon import (
    FIRED_TTL,
    PRECHECK_REFIRE_TTL,
    PRE_CHECK_MARKERS,
    VisiblePrompt,
    _command_substring_match,
    _correlate,
    _load_waiting_sessions,
    _normalize_capture_for_cache,
    _normalize_for_hash,
    _parse_visible_prompt,
    derive_pane_key,
    mark_fired,
    was_recently_fired,
)


class _FakeAppState:
    pass


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


# ── _normalize_for_hash ──────────────────────────────────────────────────

class TestNormalizeForHash:
    def test_strips_box_chars(self):
        h1 = _normalize_for_hash("│ Do you want to proceed? │")
        h2 = _normalize_for_hash("Do you want to proceed?")
        assert h1 == h2

    def test_collapses_whitespace(self):
        h1 = _normalize_for_hash("Run\n\n  pytest    -q")
        h2 = _normalize_for_hash("run pytest -q")
        assert h1 == h2

    def test_lowercases(self):
        assert _normalize_for_hash("PYTEST") == _normalize_for_hash("pytest")

    def test_stable_across_repeated_calls(self):
        text = "│ Bash command  │\n│  ❯ 1. Yes  │\n│    2. No   │"
        assert _normalize_for_hash(text) == _normalize_for_hash(text)


class TestNormalizeCaptureForCache:
    """v=437 cache normalization: stable across spinner ticks but
    invalidates when actual content changes."""

    def test_strips_spinner_timer_lines(self):
        text = (
            "● Bash(echo hi)\n"
            "  ⎿  hi\n"
            "✻ Brewed for 36s · ↓ 750 tokens\n"
            "❯ \n"
        )
        normalized = _normalize_capture_for_cache(text)
        assert "Brewed for 36s" not in normalized
        assert "Bash(echo hi)" in normalized
        assert "❯ " in normalized

    def test_stable_across_spinner_advance(self):
        """Same content, different spinner timer → same hash."""
        t1 = (
            "● Bash(echo hi)\n"
            "✻ Brewed for 36s · ↓ 750 tokens\n"
            "❯ 1. Yes\n  2. No\n"
        )
        t2 = (
            "● Bash(echo hi)\n"
            "✻ Brewed for 38s · ↓ 770 tokens\n"
            "❯ 1. Yes\n  2. No\n"
        )
        assert _normalize_capture_for_cache(t1) == _normalize_capture_for_cache(t2)

    def test_invalidates_on_real_content_change(self):
        """A new agent output line MUST cause a different hash —
        otherwise the cache would mask real updates."""
        t1 = "● Bash(echo hi)\n✻ Brewed for 36s\n"
        t2 = "● Bash(echo hi)\n● Bash(echo bye)\n✻ Brewed for 36s\n"
        assert _normalize_capture_for_cache(t1) != _normalize_capture_for_cache(t2)

    def test_strips_spinner_glyphs(self):
        # ✻ ✶ ✷ ✢ are spinners. ● ◯ are NOT — they're agent tool/output
        # markers ("● Bash(...)") and must survive normalization or the
        # cache would mask real activity.
        for glyph in ("✻", "✶", "✷", "✢"):
            text = f"{glyph} doing something… (12s)\nreal content\n"
            normalized = _normalize_capture_for_cache(text)
            assert "doing something" not in normalized
            assert "real content" in normalized

    def test_preserves_tool_markers(self):
        """● and ◯ are tool/output markers — must NOT be stripped."""
        text = "● Bash(echo hi)\n  ⎿  hi\n● Edit(/file.py)\n"
        normalized = _normalize_capture_for_cache(text)
        assert "● Bash(echo hi)" in normalized
        assert "● Edit(/file.py)" in normalized

    def test_handles_empty_input(self):
        assert _normalize_capture_for_cache("") == ""


# ── _command_substring_match ─────────────────────────────────────────────

class TestCommandSubstringMatch:
    def test_visible_contains_jsonl(self):
        assert _command_substring_match(
            visible_body="pytest -q tests/",
            jsonl_target="pytest -q",
        )

    def test_jsonl_contains_visible(self):
        # JSONL has the full command, visible was truncated by terminal width
        assert _command_substring_match(
            visible_body="cd /very/long/path && pytest",
            jsonl_target="cd /very/long/path && pytest -q tests/test_drivers.py",
        )

    def test_unrelated_commands_no_match(self):
        assert not _command_substring_match(
            visible_body="rm -rf /tmp/foo",
            jsonl_target="git status",
        )

    def test_empty_inputs_no_match(self):
        assert not _command_substring_match("", "pytest")
        assert not _command_substring_match("pytest", "")
        assert not _command_substring_match("", "")

    def test_case_insensitive(self):
        assert _command_substring_match("PYTEST -Q", "pytest -q")

    def test_whitespace_tolerant(self):
        assert _command_substring_match(
            visible_body="pytest    -q",
            jsonl_target="pytest -q",
        )


# ── _parse_visible_prompt ────────────────────────────────────────────────

class TestParseVisiblePrompt:
    def test_two_option_prompt(self):
        text = (
            "│ Bash command                            │\n"
            "│   pytest -q                             │\n"
            "│                                         │\n"
            "│ Do you want to proceed?                 │\n"
            "│ ❯ 1. Yes                                │\n"
            "│   2. No                                 │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert "do you want to proceed" in p.question.lower()
        assert "pytest -q" in p.body.lower()
        assert not p.has_pre_check_marker

    def test_three_option_prompt(self):
        text = (
            "│ Edit                                    │\n"
            "│   /home/me/dev/file.py                  │\n"
            "│                                         │\n"
            "│ Do you want to proceed?                 │\n"
            "│ ❯ 1. Yes                                │\n"
            "│   2. Yes, and don't ask again           │\n"
            "│   3. No                                 │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert "/home/me/dev/file.py" in p.body

    def test_pre_check_marker_file_redirect(self):
        text = (
            "│ Bash command                            │\n"
            "│   echo hi > /tmp/x                      │\n"
            "│ contains file_redirect                  │\n"
            "│ ❯ 1. Yes                                │\n"
            "│   2. No                                 │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert p.has_pre_check_marker

    def test_pre_check_marker_command_substitution(self):
        text = (
            "│ Bash command                            │\n"
            "│   echo $(date)                          │\n"
            "│ contains command_substitution           │\n"
            "│ ❯ 1. Yes                                │\n"
            "│   2. No                                 │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert p.has_pre_check_marker

    def test_pre_check_marker_unhandled_node(self):
        text = (
            "│ Unhandled node type: file_redirect      │\n"
            "│ ❯ 1. Yes                                │\n"
            "│   2. No                                 │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert p.has_pre_check_marker

    def test_picks_last_selector_not_first_when_two_prompts_visible(self):
        # Production bug 2026-04-25: pane 2:0 had an OLD answered
        # pre-check ("hide arguments from path validation" + Yes/No) in
        # scrollback above a LIVE 3-option PGPASSWORD prompt. Parser
        # locked onto the OLD selector, computed a precheck stable_id
        # from the OLD body, fired once, then v=422 dedup blocked all
        # subsequent ticks — live prompt waited forever.
        text = (
            "Newline followed by # inside a quoted argument can hide\n"
            "arguments from path validation\n"
            "\n"
            "Do you want to proceed?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "\n"
            "Esc to cancel\n"
            "● Now reset the doc and re-run:\n"
            "● Bash(PGPASSWORD=password psql ...)\n"
            "  ⎿  UPDATE 1\n"
            "\n"
            "──────────\n"
            " Bash command\n"
            "   PGPASSWORD=password psql -P pager=off -c \"SELECT * FROM x\"\n"
            "   Live select\n"
            "\n"
            " This command requires approval\n"
            "\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. Yes, and don't ask again for: PGPASSWORD=password psql *\n"
            "   3. No\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        # Body must come from the LIVE prompt (PGPASSWORD), not the OLD
        # warning. If body is built around the wrong selector, this fails.
        assert "PGPASSWORD" in p.body
        assert "hide arguments" not in p.body
        # has_pre_check_marker must be False — the marker was for the OLD
        # prompt; the live one has none. Otherwise daemon fires Case 2
        # with a precheck stable_id and the v=422 dedup blocks future
        # ticks (the bug).
        assert not p.has_pre_check_marker

    def test_pre_check_marker_cross_project_read(self):
        # Real prompt reported by user 2026-04-25: 3-option Bash where
        # the command reads a path outside the active project. JSONL
        # silent during the wait, daemon Case 2 must catch it.
        text = (
            "│ Bash command                                          │\n"
            "│   latest=$(ls -t ~/.claude/projects/*.jsonl | head -1)│\n"
            "│   ./venv/bin/python -c 'print($latest)'               │\n"
            "│ Do you want to proceed?                               │\n"
            "│ ❯ 1. Yes                                              │\n"
            "│   2. Yes, allow reading from -home-gcb-dev-other/ from this project │\n"
            "│   3. No                                               │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert p.has_pre_check_marker

    def test_pre_check_marker_incomplete_fragment(self):
        # Real prompt reported by user 2026-04-25 (heredoc psql):
        #   PGPASSWORD=password psql ... <<'SQL' ... SQL
        # triggered "Command appears to be an incomplete fragment"
        # warning + 2-option Yes/No. JSONL silent during the wait, so
        # daemon Case 2 must catch it via the marker.
        text = (
            "│ Bash command                                          │\n"
            "│   PGPASSWORD=password psql ... <<'SQL'                │\n"
            "│   SELECT ... FROM documents;                          │\n"
            "│   SQL                                                 │\n"
            "│ Command appears to be an incomplete fragment          │\n"
            "│ Do you want to proceed?                               │\n"
            "│ ❯ 1. Yes                                              │\n"
            "│   2. No                                               │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert p.has_pre_check_marker

    def test_pre_check_marker_hide_arguments_path_validation(self):
        # Real prompt reported by user 2026-04-25 (QR PNG generation):
        # the multi-line python -c command triggered a "hide arguments
        # from path validation" warning that wasn't in our marker list,
        # so daemon kept skipping in Case 4 (visible-only-no-marker).
        text = (
            "│ Bash command                            │\n"
            "│   ../../.venv/bin/python -c \"...\"       │\n"
            "│ Newline followed by # inside a quoted argument can hide │\n"
            "│ arguments from path validation          │\n"
            "│ Do you want to proceed?                 │\n"
            "│ ❯ 1. Yes                                │\n"
            "│   2. No                                 │\n"
        )
        p = _parse_visible_prompt(text)
        assert p is not None
        assert p.has_pre_check_marker

    def test_no_selector_returns_none(self):
        # A bare question without the "❯ N." selector is agent prose, not a prompt.
        text = "Do you want to proceed? I think we should run pytest first.\n"
        assert _parse_visible_prompt(text) is None

    def test_no_question_or_marker_returns_none(self):
        # A numbered list in agent output should NOT be detected as a perm.
        text = (
            "Here are your options:\n"
            "❯ 1. Apple\n"
            "  2. Banana\n"
            "  3. Cherry\n"
        )
        assert _parse_visible_prompt(text) is None

    def test_empty_input(self):
        assert _parse_visible_prompt("") is None
        assert _parse_visible_prompt("   \n   ") is None


# ── _correlate (the safety-critical table) ───────────────────────────────

PANE = "2:0"
REPO = "/home/me/dev/foo"


def _vis(body: str, *, has_marker: bool = False, question: str = "Do you want to proceed?") -> VisiblePrompt:
    return VisiblePrompt(
        question=question,
        body=body,
        full_text=f"{body}\n{question}\n❯ 1. Yes\n  2. No",
        has_pre_check_marker=has_marker,
    )


class TestCorrelate:

    # Case 1: JSONL + visible correlate by command substring → real perm.
    def test_jsonl_and_visible_correlated_uses_tool_use_id(self):
        jsonl = {"id": "toolu_01ABC", "name": "Bash", "target": "pytest -q"}
        visible = _vis(body="pytest -q tests/")
        perm = _correlate(PANE, REPO, jsonl, visible)
        assert perm is not None
        assert perm.stable_id == "toolu_01ABC"
        assert perm.signal == "jsonl_visible_correlated"
        assert perm.tool == "Bash"
        assert perm.target == "pytest -q"

    # Case 2: Visible-only with PRE-CHECK marker + no JSONL → content hash.
    def test_pre_check_visible_only_uses_content_hash(self):
        visible = _vis(body="echo hi > /tmp/x", has_marker=True)
        perm = _correlate(PANE, REPO, None, visible)
        assert perm is not None
        assert perm.stable_id.startswith("precheck:")
        assert len(perm.stable_id) == len("precheck:") + 16
        assert perm.signal == "pre_check_visible_only"
        assert perm.tool == "Bash"

    # Case 3: JSONL unresolved, no visible → tool executing, skip.
    def test_jsonl_only_no_visible_skips(self):
        jsonl = {"id": "toolu_01XYZ", "name": "Bash", "target": "sleep 30"}
        assert _correlate(PANE, REPO, jsonl, None) is None

    # Case 4: Visible only WITHOUT marker → stale scrollback, skip.
    # This was the v=409 disaster path — never re-introduce.
    def test_visible_only_without_marker_skips(self):
        visible = _vis(body="pytest -q", has_marker=False)
        assert _correlate(PANE, REPO, None, visible) is None

    # Case 5: JSONL + visible but commands don't match → wrong correlation.
    def test_jsonl_and_visible_mismatch_skips(self):
        jsonl = {"id": "toolu_01ABC", "name": "Bash", "target": "git status"}
        visible = _vis(body="rm -rf /tmp/foo")
        perm = _correlate(PANE, REPO, jsonl, visible)
        # Mismatch: visible is for a DIFFERENT operation than the unresolved
        # tool_use. Firing here would auto-approve the wrong command.
        assert perm is None

    # Case 1b: PRE-CHECK marker + JSONL Bash unresolved → trust JSONL.
    # Production bug 2026-04-25: PGPASSWORD heredoc psql rendered
    # "Command appears to be an incomplete fragment" with JSONL holding
    # the Bash tool_use. The visible body was the warning text, NOT the
    # psql command — substring match failed and daemon returned None.
    # User had to manually tap "1". This case covers the combined pattern.
    def test_jsonl_bash_with_precheck_marker_uses_tool_use_id(self):
        jsonl = {
            "id": "toolu_01XYZ",
            "name": "Bash",
            "target": "PGPASSWORD=password psql -h 127.0.0.1 -d sb <<'SQL' SELECT * FROM x; SQL",
        }
        # Visible body shows the warning, not the command — typical of
        # pre-check render: Claude wraps the command in a box but the
        # parsed body lines lead with the warning.
        visible = _vis(
            body="Bash command appears to be an incomplete fragment",
            has_marker=True,
        )
        perm = _correlate(PANE, REPO, jsonl, visible)
        assert perm is not None
        assert perm.stable_id == "toolu_01XYZ"
        assert perm.signal == "jsonl_visible_correlated_precheck"
        assert perm.tool == "Bash"
        assert "PGPASSWORD" in perm.target

    # Safety: pre-check marker must NOT override Case 1 mismatch when
    # the JSONL tool is NOT Bash (only Bash has pre-check warnings;
    # Edit/Write/Read mismatches are still real "wrong correlation").
    def test_jsonl_non_bash_with_precheck_marker_still_skips(self):
        jsonl = {
            "id": "toolu_01EDIT",
            "name": "Edit",
            "target": "/home/me/file.py",
        }
        visible = _vis(
            body="Bash command appears to be an incomplete fragment",
            has_marker=True,
        )
        perm = _correlate(PANE, REPO, jsonl, visible)
        # Edit + Bash precheck = wrong correlation; don't fire.
        assert perm is None

    # Both empty → nothing to do.
    def test_no_jsonl_no_visible_skips(self):
        assert _correlate(PANE, REPO, None, None) is None

    # Case 3: sessions/{pid}.json says waiting → fire from visible
    # without requiring a marker. The session-file signal is the most
    # authoritative — Claude sets it the moment a permission prompt
    # is rendered, BEFORE the tool_use lands in JSONL.
    def test_case3_session_waiting_fires_without_marker(self):
        visible = _vis(body="PGPASSWORD=password psql -c 'select 1'", has_marker=False)
        session = {
            "sessionId": "abc-123",
            "pid": 650,
            "waitingFor": "approve Bash",
            "tool": "Bash",
            "updatedAt": 1700000000000,
        }
        perm = _correlate(PANE, REPO, None, visible, session_waiting=session)
        assert perm is not None
        assert perm.signal == "session_waiting"
        assert perm.tool == "Bash"
        assert perm.stable_id.startswith("precheck:")
        # Stable across re-scans (PRECHECK_REFIRE_TTL dedup applies)

    def test_case3_session_waiting_uses_tool_from_waitingFor(self):
        """waitingFor=\"approve Edit\" → tool=Edit. Not hardcoded to Bash."""
        visible = _vis(body="/path/to/file.py", has_marker=False)
        session = {"tool": "Edit", "waitingFor": "approve Edit", "updatedAt": 1700000000000}
        perm = _correlate(PANE, REPO, None, visible, session_waiting=session)
        assert perm is not None
        assert perm.tool == "Edit"

    def test_case3_no_visible_skips(self):
        """Session says waiting but pane has no prompt rendered → don't
        fire. Could be a transient state or a freshly-completed prompt."""
        session = {"tool": "Bash", "waitingFor": "approve Bash", "updatedAt": 1700000000000}
        assert _correlate(PANE, REPO, None, None, session_waiting=session) is None

    def test_case3_no_session_skips(self):
        """No session_waiting → don't fire from visible alone (no marker
        either). This is Case 4 baseline (visible-only, no marker)."""
        visible = _vis(body="some command", has_marker=False)
        assert _correlate(PANE, REPO, None, visible, session_waiting=None) is None

    def test_case1_wins_over_case3_when_substring_matches(self):
        """If JSONL has a real tool_use that matches visible body,
        Case 1 fires with the tool_use_id (more reliable than precheck:hash).
        Case 3 only fires when Case 1 mismatches or has no JSONL."""
        jsonl = {"id": "toolu_01ABC", "name": "Bash", "target": "git status"}
        visible = _vis(body="git status")
        session = {"tool": "Bash", "waitingFor": "approve Bash", "updatedAt": 1700000000000}
        perm = _correlate(PANE, REPO, jsonl, visible, session_waiting=session)
        assert perm is not None
        assert perm.stable_id == "toolu_01ABC"
        assert perm.signal == "jsonl_visible_correlated"

    def test_case3_takes_over_when_case1_mismatches(self):
        """JSONL has stale unresolved tool_use, visible is for a NEW
        prompt, sessions confirms waiting → Case 3 fires from visible
        (Case 1's "wrong correlation" guard is overridden by sessions/)."""
        jsonl = {"id": "toolu_old", "name": "Bash", "target": "git log"}
        visible = _vis(body="PGPASSWORD=password psql ...")
        session = {"tool": "Bash", "waitingFor": "approve Bash", "updatedAt": 1700000000000}
        perm = _correlate(PANE, REPO, jsonl, visible, session_waiting=session)
        assert perm is not None
        assert perm.signal == "session_waiting"
        assert perm.tool == "Bash"

    def test_case3_jsonl_mismatch_no_session_still_skips(self):
        """Without session_waiting, Case 1 mismatch still returns None
        (the v=409-class safety we never want to remove)."""
        jsonl = {"id": "toolu_old", "name": "Bash", "target": "git log"}
        visible = _vis(body="rm -rf /tmp")
        assert _correlate(PANE, REPO, jsonl, visible, session_waiting=None) is None


# ── stable_id stability ──────────────────────────────────────────────────

class TestStableIdStability:
    def test_pre_check_id_stable_across_repeated_scans(self):
        """The same prompt re-rendered (cursor blink, status tick) must
        produce the same stable_id — otherwise dedup fails and we fire
        repeatedly."""
        v1 = _vis(body="echo hi > /tmp/x", has_marker=True)
        # Simulate a re-render: extra whitespace, different box-line spacing
        v2 = VisiblePrompt(
            question=v1.question,
            body=v1.body,
            full_text="│  " + v1.full_text.replace("\n", "  \n│ "),
            has_pre_check_marker=True,
        )
        p1 = _correlate(PANE, REPO, None, v1)
        p2 = _correlate(PANE, REPO, None, v2)
        assert p1 is not None and p2 is not None
        assert p1.stable_id == p2.stable_id

    def test_pre_check_id_differs_for_different_commands(self):
        v1 = _vis(body="echo hi > /tmp/x", has_marker=True)
        v2 = _vis(body="echo bye > /tmp/y", has_marker=True)
        p1 = _correlate(PANE, REPO, None, v1)
        p2 = _correlate(PANE, REPO, None, v2)
        assert p1 is not None and p2 is not None
        assert p1.stable_id != p2.stable_id

    def test_jsonl_id_stable_across_calls(self):
        """When JSONL has the tool_use_id, repeated correlation returns
        the same id — the source of truth is JSONL, not the visible text."""
        jsonl = {"id": "toolu_01ABC", "name": "Bash", "target": "pytest -q"}
        v1 = _vis(body="pytest -q tests/")
        v2 = _vis(body="pytest -q tests/test_foo.py")  # different visible body, same tool_use
        p1 = _correlate(PANE, REPO, jsonl, v1)
        p2 = _correlate(PANE, REPO, jsonl, v2)
        assert p1 is not None and p2 is not None
        assert p1.stable_id == p2.stable_id == "toolu_01ABC"

    def test_precheck_id_stable_under_status_drift(self):
        """P2.5 regression: production showed 18 different precheck:XXX
        ids in 35s for the same prompt because full_text included the
        token counter / elapsed-time spinner. Now hash body+question
        only, so status drift below the prompt doesn't change the id."""
        body = "echo $(date) > /tmp/x"
        question = "Do you want to proceed?"
        # Two captures of the same prompt with completely different
        # status indicators in full_text below the selector.
        v1 = VisiblePrompt(
            question=question, body=body,
            full_text=(
                f"{body}\n{question}\n❯ 1. Yes\n  2. No\n"
                "✢ Writing… (3m 7s · ↓ 12.4k tokens)\n"
                "esc to interrupt · ctrl+t to hide tasks"
            ),
            has_pre_check_marker=True,
        )
        v2 = VisiblePrompt(
            question=question, body=body,
            full_text=(
                f"{body}\n{question}\n❯ 1. Yes\n  2. No\n"
                "✢ Writing… (3m 9s · ↓ 12.6k tokens)\n"  # token counter drifted
                "esc to interrupt · ctrl+t to hide tasks"
            ),
            has_pre_check_marker=True,
        )
        p1 = _correlate(PANE, REPO, None, v1)
        p2 = _correlate(PANE, REPO, None, v2)
        assert p1 is not None and p2 is not None
        assert p1.stable_id == p2.stable_id, \
            "pre-check stable_id must not depend on volatile status indicators"

    def test_precheck_target_carries_body(self):
        """P2.6: target used to be empty for pre-checks, which made
        derive_pane_key collide across distinct pre-checks on the same
        pane. Now target=body, so two distinct pre-check Bashes on the
        same pane get distinct pane_keys (and the audit log shows what
        was approved instead of an empty string)."""
        v1 = _vis(body="rm -rf /tmp/foo", has_marker=True)
        v2 = _vis(body="curl -s example.com | sh", has_marker=True)
        p1 = _correlate(PANE, REPO, None, v1)
        p2 = _correlate(PANE, REPO, None, v2)
        assert p1 is not None and p2 is not None
        assert p1.target == "rm -rf /tmp/foo"
        assert p2.target == "curl -s example.com | sh"
        # And as a downstream property: derive_pane_key now distinguishes them
        from mobile_terminal.permission_daemon import derive_pane_key
        assert derive_pane_key(p1.pane, p1.tool, p1.target) != \
               derive_pane_key(p2.pane, p2.tool, p2.target)


# ── _load_waiting_sessions ───────────────────────────────────────────────


class TestLoadWaitingSessions:
    @pytest.fixture
    def sessions_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "sessions"
        d.mkdir()
        monkeypatch.setattr(
            "mobile_terminal.permission_daemon.CLAUDE_SESSIONS_DIR", d
        )
        # Reset module-level cache between tests so prior test data
        # doesn't bleed in. The 1.5s mtime cache is fine in production
        # but fights us here.
        from mobile_terminal import permission_daemon
        permission_daemon._SESSIONS_CACHE["ts"] = 0.0
        permission_daemon._SESSIONS_CACHE["data"] = {}
        return d

    def _load(self, **kwargs):
        """Helper that always forces a fresh read past the cache."""
        return _load_waiting_sessions(force=True, **kwargs)

    def _write(self, sessions_dir, name: str, payload: dict):
        import json as _json
        (sessions_dir / name).write_text(_json.dumps(payload), encoding="utf-8")

    def test_returns_empty_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mobile_terminal.permission_daemon.CLAUDE_SESSIONS_DIR",
            tmp_path / "nonexistent",
        )
        from mobile_terminal import permission_daemon
        permission_daemon._SESSIONS_CACHE["ts"] = 0.0
        permission_daemon._SESSIONS_CACHE["data"] = {}
        assert _load_waiting_sessions(force=True) == {}

    def test_includes_waiting_session_with_live_pid(self, sessions_dir):
        import os, time as _time
        now = _time.time()
        # Use the test runner's own PID so /proc/{pid} exists.
        self._write(sessions_dir, "650.json", {
            "pid": os.getpid(),
            "sessionId": "abc",
            "cwd": "/home/me/dev/secondbrain",
            "status": "waiting",
            "waitingFor": "approve Bash",
            "updatedAt": int(now * 1000),
        })
        result = self._load(now=now)
        assert "/home/me/dev/secondbrain" in result
        entry = result["/home/me/dev/secondbrain"]
        assert entry["tool"] == "Bash"
        assert entry["waitingFor"] == "approve Bash"
        assert entry["pid"] == os.getpid()

    def test_excludes_status_busy(self, sessions_dir):
        import os, time as _time
        now = _time.time()
        self._write(sessions_dir, "1.json", {
            "pid": os.getpid(),
            "cwd": "/home/me/dev/x",
            "status": "busy",  # not waiting
            "waitingFor": "approve Bash",
            "updatedAt": int(now * 1000),
        })
        assert self._load(now=now) == {}

    def test_excludes_waitingFor_not_starting_with_approve(self, sessions_dir):
        import os, time as _time
        now = _time.time()
        self._write(sessions_dir, "1.json", {
            "pid": os.getpid(),
            "cwd": "/home/me/dev/x",
            "status": "waiting",
            "waitingFor": "user_input",  # not "approve <Tool>"
            "updatedAt": int(now * 1000),
        })
        assert self._load(now=now) == {}

    def test_excludes_dead_pid(self, sessions_dir):
        """Session files persist on disk after Claude exits. v=448:
        stale-process protection is now PID-aliveness via /proc/{pid}
        (was a 30s updatedAt cutoff, which incorrectly filtered live
        long-pending waits — confirmed 2026-04-26 deploy-web prompt
        pending 17min, sessions file age 75s, daemon never fired)."""
        import time as _time
        now = _time.time()
        # PID well outside the realistic range — /proc/99999999 does not exist.
        self._write(sessions_dir, "1.json", {
            "pid": 99999999,
            "cwd": "/home/me/dev/x",
            "status": "waiting",
            "waitingFor": "approve Bash",
            "updatedAt": int(now * 1000),
        })
        assert self._load(now=now) == {}

    def test_includes_long_pending_with_live_pid(self, sessions_dir):
        """A waiting prompt that's been pending >30s must STILL be
        returned as long as the process is alive. This is the bug the
        v=448 PID switch fixes — long human-approval waits were being
        filtered as 'stale'."""
        import os, time as _time
        now = _time.time()
        self._write(sessions_dir, "650.json", {
            "pid": os.getpid(),
            "cwd": "/home/me/dev/secondbrain",
            "status": "waiting",
            "waitingFor": "approve Bash",
            "updatedAt": int((now - 600) * 1000),  # 10 minutes ago
        })
        result = self._load(now=now)
        assert "/home/me/dev/secondbrain" in result

    def test_handles_corrupt_json_gracefully(self, sessions_dir):
        (sessions_dir / "broken.json").write_text("not json {", encoding="utf-8")
        # Other valid sessions in the same dir should still load.
        import os, time as _time
        now = _time.time()
        self._write(sessions_dir, "good.json", {
            "pid": os.getpid(),
            "cwd": "/home/me/dev/y",
            "status": "waiting",
            "waitingFor": "approve Edit",
            "updatedAt": int(now * 1000),
        })
        result = self._load(now=now)
        assert "/home/me/dev/y" in result

    def test_cache_serves_repeated_calls_within_ttl(self, sessions_dir):
        """v=433 added a 1.5s cache to amortize the cost of ~6-10
        calls/sec across daemon ticks + scanner ticks + the
        /api/permissions/waiting endpoint. Repeated calls within the
        TTL must return the same dict reference (cache hit) without
        rescanning the filesystem."""
        import os, time as _time
        now = _time.time()
        self._write(sessions_dir, "1.json", {
            "pid": os.getpid(),
            "cwd": "/home/me/dev/x",
            "status": "waiting",
            "waitingFor": "approve Bash",
            "updatedAt": int(now * 1000),
        })
        # First call populates cache (force=True, then implicit recache)
        first = _load_waiting_sessions(now=now, force=True)
        assert "/home/me/dev/x" in first
        # Add a NEW session file
        self._write(sessions_dir, "2.json", {
            "pid": os.getpid(),
            "cwd": "/home/me/dev/y",
            "status": "waiting",
            "waitingFor": "approve Edit",
            "updatedAt": int(now * 1000),
        })
        # Within TTL, cache should serve the OLD result (no /y)
        cached = _load_waiting_sessions(now=now + 0.5)
        assert "/home/me/dev/y" not in cached
        # force=True bypasses cache and sees both
        fresh = _load_waiting_sessions(now=now + 0.5, force=True)
        assert "/home/me/dev/x" in fresh
        assert "/home/me/dev/y" in fresh


# ── PRE_CHECK_MARKERS contract ───────────────────────────────────────────

class TestPreCheckMarkers:
    def test_known_markers_present(self):
        # If any of these are removed by accident, pre-check prompts will
        # silently revert to "skip" (Case 4) and stop being detected.
        for required in (
            "unhandled node type:",
            "command_substitution",
            "file_redirect",
        ):
            assert any(required in m for m in PRE_CHECK_MARKERS), \
                f"PRE_CHECK_MARKERS missing required substring: {required}"


# ── derive_pane_key (cross-detector dedup key) ───────────────────────────

class TestDerivePaneKey:
    def test_same_inputs_same_key(self):
        k1 = derive_pane_key("2:0", "Bash", "pytest -q")
        k2 = derive_pane_key("2:0", "Bash", "pytest -q")
        assert k1 == k2

    def test_different_pane_different_key(self):
        a = derive_pane_key("2:0", "Bash", "pytest -q")
        b = derive_pane_key("3:0", "Bash", "pytest -q")
        assert a != b

    def test_different_tool_different_key(self):
        a = derive_pane_key("2:0", "Bash", "pytest -q")
        b = derive_pane_key("2:0", "Edit", "pytest -q")
        assert a != b

    def test_different_target_different_key(self):
        a = derive_pane_key("2:0", "Bash", "pytest -q")
        b = derive_pane_key("2:0", "Bash", "rm -rf /tmp")
        assert a != b

    def test_format_includes_pane_and_tool(self):
        # The key format is part of the contract — debugging / log greps
        # depend on it. If you change the format, update this test
        # consciously (and check log readability).
        k = derive_pane_key("2:0", "Bash", "pytest -q")
        assert k.startswith("pane:2:0:Bash:")


# ── mark_fired / was_recently_fired (shared dedup) ───────────────────────

class TestSharedDedup:
    def test_unmarked_key_not_fired(self):
        app = _FakeApp()
        assert not was_recently_fired(app, "toolu_01ABC")

    def test_marked_key_is_fired(self):
        app = _FakeApp()
        mark_fired(app, "toolu_01ABC")
        assert was_recently_fired(app, "toolu_01ABC")

    def test_multiple_keys_any_match_fires(self):
        """Daemon stamps both stable_id AND pane_key on fire; scanner only
        knows the pane_key. was_recently_fired must return True if EITHER
        key was stamped."""
        app = _FakeApp()
        mark_fired(app, "toolu_01ABC", "pane:2:0:Bash:abc123")
        # Scanner-style check: only the pane_key
        assert was_recently_fired(app, "pane:2:0:Bash:abc123")
        # Daemon-style check: only the stable_id
        assert was_recently_fired(app, "toolu_01ABC")
        # Either-or check (typical caller pattern)
        assert was_recently_fired(app, "toolu_01ABC", "unrelated_key")
        assert was_recently_fired(app, "unrelated_key", "pane:2:0:Bash:abc123")

    def test_unrelated_keys_not_fired(self):
        app = _FakeApp()
        mark_fired(app, "toolu_01ABC")
        assert not was_recently_fired(app, "toolu_OTHER")
        assert not was_recently_fired(app, "pane:2:0:Bash:xyz")

    def test_empty_keys_ignored(self):
        """Daemon may pass an empty string when stable_id or pane_key
        isn't available (e.g. precheck without target). Empty string
        must NEVER count as a positive dedup hit, otherwise a single
        empty-string fire would suppress every subsequent perm."""
        app = _FakeApp()
        mark_fired(app, "")
        assert not was_recently_fired(app, "")
        assert not was_recently_fired(app, "", "")
        # And marking with a real key still works
        mark_fired(app, "real_key")
        assert was_recently_fired(app, "real_key")
        assert not was_recently_fired(app, "")

    def test_ttl_expiry(self):
        app = _FakeApp()
        mark_fired(app, "toolu_01ABC")
        assert was_recently_fired(app, "toolu_01ABC", ttl=1.0)
        # Manually backdate the entry past ttl
        app.state.fired_perms["toolu_01ABC"] = time.time() - 60.0
        assert not was_recently_fired(app, "toolu_01ABC", ttl=1.0)
        assert not was_recently_fired(app, "toolu_01ABC", ttl=30.0)
        # But a longer ttl still finds it
        assert was_recently_fired(app, "toolu_01ABC", ttl=120.0)

    def test_default_ttl_is_fired_ttl(self):
        """Default ttl must be FIRED_TTL — if a caller forgets to pass
        ttl, the system should still apply the documented dedup window."""
        app = _FakeApp()
        mark_fired(app, "k")
        # Just past FIRED_TTL → not fired with default
        app.state.fired_perms["k"] = time.time() - (FIRED_TTL + 1)
        assert not was_recently_fired(app, "k")
        # Just within FIRED_TTL → fired with default
        app.state.fired_perms["k"] = time.time() - (FIRED_TTL / 2)
        assert was_recently_fired(app, "k")

    def test_state_attribute_lazy_init(self):
        """fired_perms doesn't need to exist on app.state before first
        use — mark_fired must initialize it. (Server startup wiring may
        run before any detector touches the dict.)"""
        app = _FakeApp()
        assert not hasattr(app.state, "fired_perms")
        mark_fired(app, "k")
        assert hasattr(app.state, "fired_perms")
        assert "k" in app.state.fired_perms

    def test_was_recently_fired_with_no_state_returns_false(self):
        """Symmetric to lazy-init: was_recently_fired called before any
        mark_fired must not raise."""
        app = _FakeApp()
        assert not was_recently_fired(app, "anything")

    def test_precheck_refire_ttl_is_longer_than_fired_ttl(self):
        """Pre-check stable_ids need a much longer dedup window than
        the cross-detector FIRED_TTL. Production bug 2026-04-25:
        precheck:8cbd80b re-fired exactly 31s after the first fire
        (TTL rollover) and landed in chat input. PRECHECK_REFIRE_TTL
        must be long enough that stale scrollback can't trigger a
        re-fire of the same content hash."""
        assert PRECHECK_REFIRE_TTL > FIRED_TTL
        # Sanity: at least 5x — if someone tunes one without the other
        # the gap closes and the bug returns.
        assert PRECHECK_REFIRE_TTL >= FIRED_TTL * 5

    def test_precheck_dedup_survives_fired_ttl_rollover(self):
        """The actual production scenario: stamp a precheck id at T,
        backdate it to T+31s (past FIRED_TTL but within
        PRECHECK_REFIRE_TTL), confirm both checks behave correctly:
          - was_recently_fired with default ttl: returns False (rollover OK)
          - was_recently_fired with PRECHECK_REFIRE_TTL: returns True (blocks re-fire)"""
        app = _FakeApp()
        mark_fired(app, "precheck:8cbd80b")
        # Simulate 31s elapsed
        app.state.fired_perms["precheck:8cbd80b"] = time.time() - 31.0
        # Default ttl (cross-detector) — entry has expired
        assert not was_recently_fired(app, "precheck:8cbd80b")
        # Long ttl (precheck stale-scrollback prevention) — entry still active
        assert was_recently_fired(app, "precheck:8cbd80b", ttl=PRECHECK_REFIRE_TTL)
