"""Routes for MCP server and plugin management."""
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from mobile_terminal.helpers import (
    CLAUDE_SETTINGS_FILE, MCP_NAME_RE, MCP_MAX_ARGS_SIZE,
    PLUGIN_NAME_RE, INSTALLED_PLUGINS_FILE,
    load_claude_settings, save_claude_settings,
)

MARKETPLACES_DIR = Path.home() / ".claude" / "plugins" / "marketplaces"

logger = logging.getLogger(__name__)


def register(app: FastAPI, deps):
    """Register MCP server and plugin routes."""

    @app.get("/api/mcp-servers")
    async def list_mcp_servers(_auth=Depends(deps.verify_token)):
        """List MCP servers from ~/.claude/settings.json."""

        settings, error = load_claude_settings()
        servers = settings.get("mcpServers", {})
        return {
            "servers": servers,
            "source": str(CLAUDE_SETTINGS_FILE),
            "error": error,
        }

    @app.post("/api/mcp-servers")
    async def add_mcp_server(request: Request, _auth=Depends(deps.verify_token)):
        """Add or update an MCP server in ~/.claude/settings.json (upsert)."""

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        name = (body.get("name") or "").strip()
        command = (body.get("command") or "").strip()
        args = body.get("args", [])
        env = body.get("env")

        # Validate name
        if not name or not MCP_NAME_RE.match(name):
            return JSONResponse(
                {"error": f"Invalid name: must match {MCP_NAME_RE.pattern}"},
                status_code=400,
            )

        # Validate command
        if not command:
            return JSONResponse({"error": "Command is required"}, status_code=400)

        # Validate args
        if not isinstance(args, list):
            return JSONResponse({"error": "Args must be an array"}, status_code=400)
        args_size = len(json.dumps(args))
        if args_size > MCP_MAX_ARGS_SIZE:
            return JSONResponse(
                {"error": f"Args too large ({args_size} bytes, max {MCP_MAX_ARGS_SIZE})"},
                status_code=400,
            )

        # Validate env
        if env is not None and not isinstance(env, dict):
            return JSONResponse({"error": "Env must be an object"}, status_code=400)

        # Load settings — refuse to write if file is corrupt
        settings, error = load_claude_settings()
        if error:
            return JSONResponse({"error": error}, status_code=409)

        # Upsert
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}
        updated = name in settings["mcpServers"]
        entry = {"command": command, "args": args}
        if env:
            entry["env"] = env
        settings["mcpServers"][name] = entry

        try:
            save_claude_settings(settings)
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        action = "updated" if updated else "added"
        logger.info(f"MCP server {action}: {name}")
        app.state.audit_log.log(
            "mcp_server_add",
            {"name": name, "command": command, "updated": updated},
        )

        return {"success": True, "name": name, "updated": updated}

    @app.delete("/api/mcp-servers/{name}")
    async def remove_mcp_server(name: str, _auth=Depends(deps.verify_token)):
        """Remove an MCP server from ~/.claude/settings.json."""

        if not MCP_NAME_RE.match(name):
            return JSONResponse({"error": "Invalid server name"}, status_code=400)

        settings, error = load_claude_settings()
        if error:
            return JSONResponse({"error": error}, status_code=409)

        servers = settings.get("mcpServers", {})
        if name not in servers:
            return JSONResponse({"error": f"Server '{name}' not found"}, status_code=404)

        del settings["mcpServers"][name]
        # Clean up empty mcpServers key
        if not settings["mcpServers"]:
            del settings["mcpServers"]

        try:
            save_claude_settings(settings)
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        logger.info(f"MCP server removed: {name}")
        app.state.audit_log.log("mcp_server_remove", {"name": name})

        return {"success": True, "name": name}

    # --- Plugin Management ---

    @app.get("/api/plugins")
    async def list_plugins(_auth=Depends(deps.verify_token)):
        """List enabled plugins and installed plugin IDs."""

        settings, error = load_claude_settings()
        enabled = settings.get("enabledPlugins", {})

        # Read installed plugins for discovery
        installed = []
        try:
            data = json.loads(INSTALLED_PLUGINS_FILE.read_text(encoding="utf-8"))
            installed = list(data.get("plugins", {}).keys())
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            pass

        return {
            "enabled": enabled,
            "installed": installed,
            "error": error,
        }

    @app.post("/api/plugins/toggle")
    async def toggle_plugin(request: Request, _auth=Depends(deps.verify_token)):
        """Enable or disable a plugin in ~/.claude/settings.json."""

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        name = (body.get("name") or "").strip()
        enabled = body.get("enabled", True)

        if not name or not PLUGIN_NAME_RE.match(name):
            return JSONResponse({"error": "Plugin name is required"}, status_code=400)

        settings, error = load_claude_settings()
        if error:
            return JSONResponse({"error": error}, status_code=409)

        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}

        if enabled:
            settings["enabledPlugins"][name] = True
        else:
            settings["enabledPlugins"].pop(name, None)

        try:
            save_claude_settings(settings)
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        action = "enabled" if enabled else "disabled"
        logger.info(f"Plugin {action}: {name}")
        app.state.audit_log.log(
            "plugin_toggle",
            {"name": name, "enabled": enabled},
        )

        return {"success": True, "name": name, "enabled": enabled}

    @app.get("/api/plugins/marketplace")
    async def list_marketplace_plugins(_auth=Depends(deps.verify_token)):
        """Browse plugins from locally cached marketplace directories."""

        settings, _ = load_claude_settings()
        enabled = settings.get("enabledPlugins", {})
        plugins = []

        if not MARKETPLACES_DIR.is_dir():
            return {"plugins": plugins}

        for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
            manifest = mp_dir / ".claude-plugin" / "marketplace.json"
            if not manifest.is_file():
                continue
            marketplace = mp_dir.name
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning(f"Skipping corrupt marketplace.json in {mp_dir}")
                continue

            for p in data.get("plugins", []):
                name = p.get("name", "")
                if not name:
                    continue
                plugin_id = f"{name}@{marketplace}"
                plugins.append({
                    "id": plugin_id,
                    "name": name,
                    "description": p.get("description", ""),
                    "category": p.get("category", ""),
                    "marketplace": marketplace,
                    "enabled": plugin_id in enabled,
                })

        return {"plugins": plugins}

    @app.get("/api/mcp-servers/catalog")
    async def list_mcp_catalog(_auth=Depends(deps.verify_token)):
        """Browse MCP servers from locally cached external_plugins directories."""

        settings, _ = load_claude_settings()
        configured = settings.get("mcpServers", {})
        servers = []

        if not MARKETPLACES_DIR.is_dir():
            return {"servers": servers}

        for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
            ext_dir = mp_dir / "external_plugins"
            if not ext_dir.is_dir():
                continue

            for plugin_dir in sorted(ext_dir.iterdir()):
                mcp_file = plugin_dir / ".mcp.json"
                if not mcp_file.is_file():
                    continue

                try:
                    mcp_data = json.loads(mcp_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                # Read description from .claude-plugin/plugin.json
                description = ""
                meta_file = plugin_dir / ".claude-plugin" / "plugin.json"
                if meta_file.is_file():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        description = meta.get("description", "")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass

                # Normalize: some wrap in mcpServers, some don't
                server_defs = mcp_data
                if "mcpServers" in mcp_data:
                    server_defs = mcp_data["mcpServers"]

                for name, config in server_defs.items():
                    if not isinstance(config, dict):
                        continue
                    server_type = config.get("type", "stdio")
                    if "command" in config:
                        server_type = "stdio"
                    servers.append({
                        "name": name,
                        "description": description,
                        "type": server_type,
                        "config": config,
                        "configured": name in configured,
                    })

        return {"servers": servers}
