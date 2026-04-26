"""Tests for permission_policy.evaluate, focused on the hard_guard
bypass added so trusted-repo allow rules can override the safety
check on high-risk Bash commands."""

import pytest

from mobile_terminal.permission_policy import (
    PermissionPolicy,
    classify_risk,
    normalize_request,
)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
    monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
    monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
    monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")
    return PermissionPolicy()


def _req(tool, target, repo="/home/me/dev/foo"):
    return normalize_request({"tool": tool, "target": target}, repo)


class TestHardGuardBypass:
    def test_low_risk_bash_allows_with_repo_rule(self, policy):
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow")
        d = policy.evaluate(_req("Bash", "ls -la"))
        assert d.action == "allow"
        assert d.reason == "repo_rule"

    def test_high_risk_bash_blocked_by_hard_guard_default(self, policy):
        """Plain repo allow rule does NOT bypass hard_guard for high-risk."""
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow")
        d = policy.evaluate(_req("Bash", "rm -rf /tmp/x"))
        assert d.action == "prompt"
        assert d.reason == "hard_guard"
        assert d.risk == "high"

    def test_high_risk_bash_allowed_by_bypass_flag(self, policy):
        """When the matching allow rule opts in via bypass_hard_guard, fire."""
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=True)
        d = policy.evaluate(_req("Bash", "rm -rf /tmp/x"))
        assert d.action == "allow"
        assert d.reason == "repo_rule"
        assert d.risk == "high"

    def test_bypass_does_not_apply_to_other_repos(self, policy):
        """Bypass on repo A's rule must not affect repo B's high-risk perms."""
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=True)
        d = policy.evaluate(_req("Bash", "rm -rf /tmp/x", repo="/home/me/dev/bar"))
        assert d.action == "prompt"
        assert d.reason == "hard_guard"

    def test_deny_still_wins_over_bypass(self, policy):
        """A matching deny rule still beats a bypass-flagged allow rule."""
        policy.add_rule(tool="Bash", matcher_type="command", matcher="rm -rf",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="deny")
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=True)
        d = policy.evaluate(_req("Bash", "rm -rf /tmp/x"))
        assert d.action == "deny"

    def test_bypass_field_persists_across_save_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")
        p1 = PermissionPolicy()
        p1.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                    scope="repo", scope_value="/home/me/dev/foo",
                    action="allow", bypass_hard_guard=True)
        # Reload from disk
        p2 = PermissionPolicy()
        p2.load()
        match = [r for r in p2._rules if r.scope_value == "/home/me/dev/foo"]
        assert len(match) == 1
        assert match[0].bypass_hard_guard is True

    def test_re_add_with_bypass_upgrades_existing_rule(self, policy):
        """Re-clicking 'Always allow this repo' on a legacy non-bypass rule
        upgrades the existing rule rather than failing dedup silently."""
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=False)
        # First decision blocked by hard_guard
        d1 = policy.evaluate(_req("Bash", "rm -rf /tmp/x"))
        assert d1.action == "prompt"
        # Re-add with bypass=True → should upgrade existing rule
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=True)
        d2 = policy.evaluate(_req("Bash", "rm -rf /tmp/x"))
        assert d2.action == "allow"
        assert d2.reason == "repo_rule"

    def test_re_add_without_bypass_does_not_downgrade(self, policy):
        """Once a rule has bypass=True, a later vanilla add must not revoke it."""
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=True)
        policy.add_rule(tool="Bash", matcher_type="tool_only", matcher="",
                        scope="repo", scope_value="/home/me/dev/foo",
                        action="allow", bypass_hard_guard=False)
        d = policy.evaluate(_req("Bash", "rm -rf /tmp/x"))
        assert d.action == "allow"

    def test_decide_endpoint_allow_fires_and_dedups(self, tmp_path, monkeypatch):
        """End-to-end: decide endpoint runs policy, fires y, dedups, audits."""
        from fastapi.testclient import TestClient
        from mobile_terminal.config import Config
        from mobile_terminal.server import create_app

        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        audit = tmp_path / "audit.jsonl"
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", audit)

        app = create_app(Config(session_name="test", no_auth=True))
        # Stub runtime.send_keys so the endpoint doesn't try to talk to tmux
        sent = []
        async def fake_send_keys(target, key, literal=False):
            sent.append((target, key, literal))
        app.state.runtime.send_keys = fake_send_keys
        # Seed an allow rule for this repo
        app.state.permission_policy.add_rule(
            tool="Bash", matcher_type="tool_only", matcher="",
            scope="repo", scope_value="/home/me/dev/foo",
            action="allow",
        )

        client = TestClient(app)
        body = {
            "tool": "Bash", "target": "ls -la",
            "repo": "/home/me/dev/foo", "source_pane": "2:0",
        }
        r1 = client.post("/api/permissions/decide", json=body)
        assert r1.status_code == 200
        assert r1.json()["decision"] == "allow"
        assert r1.json()["reason"] == "repo_rule"
        # Server should have sent y + Enter
        assert len(sent) == 2
        # v=408: send "1" (option-1 = Yes) instead of "y" — Claude's
        # multi-option prompts don't accept "y" as a valid choice.
        assert sent[0][1] == "1"
        assert sent[1][1] == "Enter"
        # Audit should have an entry
        assert audit.exists()
        import json as _json
        entries = [_json.loads(l) for l in audit.read_text().strip().splitlines()]
        assert any(e["decision"] == "allow" and e["tool"] == "Bash" for e in entries)

        # Second call within TTL → already_handled, no extra send_keys
        r2 = client.post("/api/permissions/decide", json=body)
        assert r2.status_code == 200
        assert r2.json()["decision"] == "already_handled"
        assert len(sent) == 2  # unchanged

    def test_decide_endpoint_needs_human_no_dedup(self, tmp_path, monkeypatch):
        """needs_human path doesn't dedup — user can be re-prompted."""
        from fastapi.testclient import TestClient
        from mobile_terminal.config import Config
        from mobile_terminal.server import create_app

        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")

        app = create_app(Config(session_name="test", no_auth=True))
        sent = []
        async def fake_send_keys(target, key, literal=False):
            sent.append((target, key, literal))
        app.state.runtime.send_keys = fake_send_keys

        client = TestClient(app)
        # No matching rule → policy returns prompt (no_match)
        body = {
            "tool": "Bash", "target": "uncommon-cmd",
            "repo": "/home/me/dev/foo", "source_pane": "2:0",
        }
        r1 = client.post("/api/permissions/decide", json=body)
        assert r1.json()["decision"] == "prompt"
        assert sent == []  # nothing fired

        # Second call: still prompts (not deduped)
        r2 = client.post("/api/permissions/decide", json=body)
        assert r2.json()["decision"] == "prompt"

    def test_decide_endpoint_high_risk_blocked_without_bypass(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from mobile_terminal.config import Config
        from mobile_terminal.server import create_app

        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")

        app = create_app(Config(session_name="test", no_auth=True))
        sent = []
        async def fake_send_keys(target, key, literal=False):
            sent.append((target, key, literal))
        app.state.runtime.send_keys = fake_send_keys
        # Allow rule WITHOUT bypass
        app.state.permission_policy.add_rule(
            tool="Bash", matcher_type="tool_only", matcher="",
            scope="repo", scope_value="/home/me/dev/foo",
            action="allow", bypass_hard_guard=False,
        )

        client = TestClient(app)
        body = {
            "tool": "Bash", "target": "rm -rf /tmp/x",
            "repo": "/home/me/dev/foo", "source_pane": "2:0",
        }
        r = client.post("/api/permissions/decide", json=body)
        assert r.json()["decision"] == "prompt"
        assert r.json()["reason"] == "hard_guard"
        assert sent == []

    def test_decide_endpoint_high_risk_bypass_fires(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from mobile_terminal.config import Config
        from mobile_terminal.server import create_app

        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")

        app = create_app(Config(session_name="test", no_auth=True))
        sent = []
        async def fake_send_keys(target, key, literal=False):
            sent.append((target, key, literal))
        app.state.runtime.send_keys = fake_send_keys
        app.state.permission_policy.add_rule(
            tool="Bash", matcher_type="tool_only", matcher="",
            scope="repo", scope_value="/home/me/dev/foo",
            action="allow", bypass_hard_guard=True,
        )

        client = TestClient(app)
        r = client.post("/api/permissions/decide", json={
            "tool": "Bash", "target": "rm -rf /tmp/x",
            "repo": "/home/me/dev/foo", "source_pane": "2:0",
        })
        assert r.json()["decision"] == "allow"
        assert r.json()["reason"] == "repo_rule"
        assert len(sent) == 2
        # v=408: send "1" (option-1 = Yes) instead of "y" — Claude's
        # multi-option prompts don't accept "y" as a valid choice.
        assert sent[0][1] == "1"

    def test_decide_endpoint_rejects_missing_fields(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from mobile_terminal.config import Config
        from mobile_terminal.server import create_app

        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")

        app = create_app(Config(session_name="test", no_auth=True))
        client = TestClient(app)
        # No tool
        r1 = client.post("/api/permissions/decide", json={"source_pane": "2:0"})
        assert r1.status_code == 400
        # No source_pane
        r2 = client.post("/api/permissions/decide", json={"tool": "Bash"})
        assert r2.status_code == 400

    def test_legacy_rule_without_field_loads_as_false(self, tmp_path, monkeypatch):
        """Existing rules in policy.json from before the field was added
        must load with bypass_hard_guard=False (safe default)."""
        import json
        legacy = {
            "mode": "safe_auto",
            "rules": [{
                "id": "legacy-id", "tool": "Bash", "matcher_type": "tool_only",
                "matcher": "", "scope": "repo",
                "scope_value": "/home/me/dev/foo", "action": "allow",
                "created_at": 1, "created_from": "banner", "note": None,
            }],
        }
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.POLICY_FILE", tmp_path / "policy.json")
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_DIR", tmp_path)
        monkeypatch.setattr("mobile_terminal.permission_policy.AUDIT_FILE", tmp_path / "audit.jsonl")
        (tmp_path / "policy.json").write_text(json.dumps(legacy))
        p = PermissionPolicy()
        p.load()
        assert len(p._rules) == 1
        assert p._rules[0].bypass_hard_guard is False
