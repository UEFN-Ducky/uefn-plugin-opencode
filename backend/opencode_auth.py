"""Zero-setup models for OpenCode: Ducky's saved keys, OpenCode's free model, Sign in.

Keys the person already saved in Ducky go to the local OpenCode server as the env names
OpenCode reads, so they never sign in twice. With no key at all OpenCode's free model
answers. Subscriptions (ChatGPT, GitHub Copilot, SuperGrok…) sign in through OpenCode's
own OAuth endpoints from a Ducky button. Nothing here is ever logged.
"""

from __future__ import annotations

import re
from typing import Any

# Ducky secret id → OpenCode env name (from GET /api/integration on OpenCode 2.2).
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "spacexai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "opencode": "OPENCODE_API_KEY",  # this gateway's own optional key (OpenCode Zen / Console)
}


def server_env() -> dict[str, str]:
    """Env for the OpenCode server: only keys that are actually saved."""
    try:
        from backend.agent.secrets import get_key
    except ImportError:
        return {}
    env: dict[str, str] = {}
    for secret, name in KEY_ENV.items():
        try:
            value = (get_key(secret) or "").strip()
        except Exception:
            value = ""
        if value:
            env[name] = value
    return env


def model_rows(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenCode Model.Info rows → Ducky picker rows (``provider/model`` ids)."""
    rows: list[dict[str, Any]] = []
    for m in models:
        if not isinstance(m, dict) or str(m.get("type") or "chat") != "chat":
            continue
        provider, mid = str(m.get("providerID") or ""), str(m.get("id") or m.get("modelID") or "")
        if not provider or not mid:
            continue
        caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
        variants = [str(v.get("id")) for v in m.get("variants") or [] if isinstance(v, dict) and v.get("id")]
        row: dict[str, Any] = {
            "id": f"{provider}/{mid}",
            "name": str(m.get("name") or mid),
            "provider": "OpenCode",
            "supports_tools": bool(caps.get("tools", True)),
        }
        if variants:
            row["thinking_menu"] = [{"id": v, "label": v.title()} for v in variants]
            row["supports_thinking_effort"] = True
        rows.append(row)
    rows.sort(key=lambda r: (not r["id"].startswith("opencode/"), r["id"]))
    return rows


# Settings' Log in button signs in the first of these OpenCode offers (subscriptions people
# already pay for). ponytail: one button, one provider; a provider picker when people ask.
PREFERRED_OAUTH = (("openai", "chatgpt-token-sharing"), ("github-copilot", "device"),
                   ("xai", "device"), ("opencode", "device"))
_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,5})\b")


def oauth_methods(client: Any) -> set[tuple[str, str]]:
    """Every (integration, method) OAuth sign-in OpenCode offers."""
    out: set[tuple[str, str]] = set()
    for row in client.integrations():
        for method in row.get("methods") or []:
            if isinstance(method, dict) and method.get("type") == "oauth" and method.get("id"):
                out.add((str(row.get("id")), str(method["id"])))
    return out


class LoginFlow:
    """Settings' login modal protocol (login → login_status polls → login_submit for a code)."""

    def __init__(self) -> None:
        self.client: Any = None
        self.integration = ""
        self.attempt = ""

    def start(self, client: Any) -> dict[str, Any]:
        offered = oauth_methods(client)
        pick = next((p for p in PREFERRED_OAUTH if p in offered), None)
        if pick is None:
            return {"ok": False, "error": "OpenCode offers no browser sign-in here. Add an API key under "
                                          "Settings → LLMs; OpenCode uses it automatically."}
        attempt = client.oauth_start(*pick)
        self.client, self.integration, self.attempt = client, pick[0], str(attempt.get("attemptID") or "")
        if not self.attempt:
            return {"ok": False, "error": "OpenCode did not start the sign-in."}
        instructions = str(attempt.get("instructions") or "")
        code = _CODE_RE.search(instructions)
        return {"ok": True, "logged_in": False, "needs_login": True,
                "login_ui": "device_code" if code else "browser",
                "auth_url": str(attempt.get("url") or ""), "user_code": code.group(1) if code else "",
                "needs_code": attempt.get("mode") == "code", "message": instructions}

    def status(self) -> dict[str, Any]:
        if not self.attempt:
            return {"ok": True, "logged_in": False}
        state = str(self.client.oauth_status(self.integration, self.attempt).get("status") or "")
        if state == "complete":
            self.attempt = ""
            return {"ok": True, "logged_in": True}
        if state in ("failed", "expired"):
            self.attempt = ""
            return {"ok": False, "logged_in": False, "error": f"Sign-in {state}. Press Log in to try again."}
        return {"ok": True, "logged_in": False, "needs_login": True}

    def submit(self, code: str) -> dict[str, Any]:
        if not self.attempt:
            return {"ok": False, "error": "No sign-in is waiting for a code."}
        self.client.oauth_complete(self.integration, self.attempt, code.strip())
        return self.status()
