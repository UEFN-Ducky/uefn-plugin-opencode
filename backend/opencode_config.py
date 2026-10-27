"""What one Ducky chat hands the shared OpenCode server: its MCP entry, permission rules, model."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# OpenCode's own permission action names (the plan agent denies "edit"; shell is "shell").
_EDIT, _SHELL = "edit", "shell"
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def chat_tag(conv_id: str) -> str:
    """Stable, MCP-safe id for a chat: ``ducky-<12 chars>``."""
    tag = re.sub(r"[^a-z0-9]", "", (conv_id or "").lower())[:12] or "chat"
    return f"ducky-{tag}"


def mcp_entry(mcp_config_path: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Ducky's ``mcpServers.uefn`` block in OpenCode's local-server shape (None when missing).

    The run's DUCKY_* identity (lanes, attribution, requested mode) rides along: the
    bridge is a child of the OpenCode server, not of this turn, so it inherits nothing.
    """
    try:
        data = json.loads(Path(mcp_config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    block = servers.get("uefn") if isinstance(servers, dict) else None
    if not isinstance(block, dict) or not block.get("command"):
        return None
    environment = {str(k): str(v) for k, v in (block.get("env") or {}).items()}
    for key, value in (env or {}).items():
        if key.startswith("DUCKY_") and value is not None and key not in environment:
            environment[key] = str(value)
    return {
        "type": "local",
        "command": [str(block["command"]), *[str(a) for a in block.get("args") or []]],
        "environment": environment,
        # The bridge's first start can take a while; tool calls may run long (builds, renders).
        "timeout": {"startup": 120_000, "catalog": 120_000, "execution": 900_000},
    }


def session_rules(conv_id: str, *, mode: str, approval: str) -> list[dict[str, str]]:
    """Session permission rules. Later rules win, as in OpenCode's own agents.

    - Only this chat's Ducky tools: every ``ducky-*`` entry is denied, then its own allowed.
    - Ducky approval modes become OpenCode's ask/allow; Ask and Plan never edit.
    """
    tag = chat_tag(conv_id)
    rules = [
        {"action": "ducky-*", "resource": "*", "effect": "deny"},
        {"action": f"{tag}*", "resource": "*", "effect": "allow"},
    ]
    if mode in ("ask", "plan"):
        rules.append({"action": _EDIT, "resource": "*", "effect": "deny"})
        rules.append({"action": _SHELL, "resource": "*", "effect": "ask"})
    elif approval == "ask":
        rules.append({"action": _EDIT, "resource": "*", "effect": "ask"})
        rules.append({"action": _SHELL, "resource": "*", "effect": "ask"})
    elif approval == "edits":
        rules.append({"action": _SHELL, "resource": "*", "effect": "ask"})
    return rules


def agent_for_mode(mode: str) -> str:
    return "plan" if mode in ("ask", "plan") else "build"


def model_ref(model: str, effort: str = "") -> dict[str, str] | None:
    """``provider/model`` → OpenCode's Model.Ref; None for the "auto"/"default" sentinels (never sent)."""
    mid = (model or "").strip()
    if not mid or mid.lower() in ("default", "auto") or "/" not in mid:
        return None
    provider, model_id = mid.split("/", 1)
    ref = {"providerID": provider, "id": model_id}
    effort = (effort or "").strip().lower()
    if effort in _EFFORTS:
        ref["variant"] = effort
    return ref


def permission_card(request: dict[str, Any], cwd: str) -> tuple[str, dict[str, Any]]:
    """An OpenCode permission request as the tool name + input Ducky's approval card knows."""
    action = str(request.get("action") or "")
    resources = [str(r) for r in request.get("resources") or []]
    meta = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    if action == _EDIT:
        first = resources[0] if resources else ""
        path = first if not first or Path(first).is_absolute() else str(Path(cwd) / first)
        return "Edit", {"file_path": path, "files": resources}
    if action == _SHELL:
        command = str(meta.get("command") or (resources[0] if resources else ""))
        return "Bash", {"command": command, "description": str(request.get("message") or "")}
    return action or "tool", {"resources": resources, "message": str(request.get("message") or "")}
