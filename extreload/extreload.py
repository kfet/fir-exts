#!/usr/bin/env python3
# ---
# name: extreload
# description: Hot-reload a named fir extension, or skills/system prompt/models, in the live session.
# modes: tui, acp
# ---
"""Tiny lever: reload an extension by name, or the session's resources
(skills, system prompt, model registry), without restarting the session."""

from __future__ import annotations

import json
from typing import Any

import fir_ext


@fir_ext.tool(
    "ext_reload",
    "Reload a named fir extension in this live session (scope=extension, "
    "default), or reload skills + system prompt + model registry "
    "(scope=resources; runs after the current turn, a change summary "
    "arrives next turn), or MCP server configs (scope=mcp; changed "
    "servers restart).",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Extension name (scope=extension)"},
            "scope": {
                "type": "string",
                "enum": ["extension", "resources", "mcp"],
                "description": "What to reload. Default extension.",
            },
        },
    },
)
def ext_reload(args: dict[str, Any], ctx) -> str:
    scope = args.get("scope") or "extension"
    if scope == "resources":
        try:
            ctx.reload_resources()
            return "resource reload scheduled: runs after this turn"
        except Exception as exc:  # noqa: BLE001
            return f"resource reload failed: {exc!r}"
    if scope == "mcp":
        try:
            return json.dumps(ctx.reload_mcp())
        except Exception as exc:  # noqa: BLE001
            return f"mcp reload failed: {exc!r}"
    name = args.get("name")
    if not name:
        return "name is required for scope=extension"
    try:
        ctx.reload_extension(name)
        return f"reload requested: {name}"
    except Exception as exc:  # noqa: BLE001
        return f"reload failed for {name}: {exc!r}"


if __name__ == "__main__":
    fir_ext.run()
