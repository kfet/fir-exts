#!/usr/bin/env python3
# ---
# name: extreload
# description: Hot-reload a named fir extension in the live session.
# modes: tui, acp
# ---
"""Tiny lever: reload an extension by name without restarting the session."""

from __future__ import annotations

from typing import Any

import fir_ext


@fir_ext.tool(
    "ext_reload",
    "Reload a named fir extension in this live session.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Extension name"}},
        "required": ["name"],
    },
)
def ext_reload(args: dict[str, Any], ctx) -> str:
    name = args["name"]
    try:
        ctx.reload_extension(name)
        return f"reload requested: {name}"
    except Exception as exc:  # noqa: BLE001
        return f"reload failed for {name}: {exc!r}"


if __name__ == "__main__":
    fir_ext.run()
