"""Smoke test: ensure all expected routes are registered after decomposition."""
from mobile_terminal.config import Config
from mobile_terminal.server import create_app


def test_route_inventory():
    """Ensure all expected routes are registered."""
    app = create_app(Config(session_name="test", no_auth=True))
    paths = {r.path for r in app.routes}

    # Key routes that must exist
    assert "/health" in paths
    assert "/ws/terminal" in paths
    assert "/api/targets" in paths
    assert "/api/team/state" in paths
    assert "/api/health/agent" in paths
    assert "/api/queue/enqueue" in paths
    assert "/api/rollback/git/status" in paths
    assert "/api/log" in paths
    assert "/api/context" in paths
    assert "/api/challenge" in paths
    assert "/api/runner/execute" in paths
    assert "/api/preview/config" in paths
    assert "/api/mcp-servers" in paths
    assert "/api/plugins/marketplace" in paths
    assert "/api/mcp-servers/catalog" in paths
    assert "/api/process/terminate" in paths
    assert "/api/push/vapid-key" in paths
    assert "/api/scratch/list" in paths
    assert "/api/scratch/store" in paths
    assert "/api/activity" in paths
    assert "/api/preview/logs" in paths
    assert "/api/preview/logs/list" in paths
    # /api/permissions/decide deleted in Phase 4 (2026-04-25) — daemon +
    # scanner own auto-fire authority via JSONL correlation; the client
    # scraper no longer POSTs to fire on its behalf.
    assert "/api/permissions/decide" not in paths
    # /api/permissions/waiting added in v=432 — client scraper queries
    # this before showing a banner to suppress false positives.
    assert "/api/permissions/waiting" in paths

    # Total route count (adjust as routers are extracted)
    builtin_paths = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
    route_count = len([r for r in app.routes
                       if hasattr(r, "methods") and r.path not in builtin_paths])
    assert route_count >= 100, f"Expected >=100 custom routes, got {route_count}"
