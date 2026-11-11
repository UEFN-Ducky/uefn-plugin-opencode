"""Zero-setup models for OpenCode: Ducky's saved keys, OpenCode's free model, Sign in.

Keys the person already saved in Ducky go to the local OpenCode server as the env names
OpenCode reads, so they never sign in twice. With no key at all OpenCode's free model
answers. Subscriptions (ChatGPT, GitHub Copilot, SuperGrok…) sign in through OpenCode's
own OAuth endpoints from a Ducky button. Nothing here is ever logged.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
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


def picked_key(secret: str) -> tuple[str, str]:
    """(account id, key) of the provider's picked key account; falls back to the plain saved key."""
    try:
        from backend.agent import accounts

        acc = accounts.pick_account(secret)
        key = (accounts.account_key(secret, acc) or "").strip()
        if key:
            return acc, key
    except Exception:
        pass
    try:
        from backend.agent.secrets import get_key

        return "default", (get_key(secret) or "").strip()
    except Exception:
        return "", ""


def server_env() -> dict[str, str]:
    """Env for the OpenCode server: only keys that are actually saved (each provider's picked one)."""
    env: dict[str, str] = {}
    for secret, name in KEY_ENV.items():
        value = picked_key(secret)[1]
        if value:
            env[name] = value
    return env


# ── accounts ────────────────────────────────────────────────────────────
# OpenCode keeps its sign-ins in ``$XDG_DATA_HOME/opencode/auth.json``: a login account is
# its own data folder, run by its own server.

DATA_ENV = "XDG_DATA_HOME"


def auth_path(config_dir: str | Path | None) -> Path:
    """The account's auth.json. Core passes None for ``default``: OpenCode's own data folder
    (``$XDG_DATA_HOME``, else ``~/.local/share``, on Windows too). ``Path(None)`` raised here,
    so Log in and Log out on the Default row failed and it always read logged out."""
    root = config_dir or os.environ.get(DATA_ENV) or Path.home() / ".local" / "share"
    return Path(root) / "opencode" / "auth.json"


def account_env(config_dir: str | None) -> dict[str, str]:
    if not config_dir:
        return {}
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    return {DATA_ENV: str(config_dir)}


def account_login(config_dir: str | None) -> dict[str, Any]:
    """Terminal fallback for cores without account_browser_login."""
    from . import opencode_install as install

    return {"argv": [install.resolve_bin("") or "opencode", "auth", "login"], "env": account_env(config_dir)}


INSTALLING = "OpenCode isn't installed yet. Ducky is installing it; sign-in starts by itself."
# ponytail: one sign-in per account in this process; a second Log in replaces it.
_login_lock = threading.Lock()
_logins: dict[str, "LoginFlow"] = {}  # account folder ("" = default) -> its running sign-in


def account_browser_login(config_dir: str | None) -> dict[str, Any]:
    """Settings' Log in, one click: OpenCode's own OAuth through Ducky's server, the link
    opened in the browser, the code (when there is one) shown in the row. No terminal tab."""
    import webbrowser

    from . import opencode_install as install
    from .opencode_server import shared_server

    binary = install.resolve_bin("")
    if not binary:
        install.install_in_background()
        return {"error": INSTALLING}
    key, extra = str(config_dir or ""), account_env(config_dir)
    flow = LoginFlow()
    try:
        client, _hub = shared_server(binary, lambda: {**server_env(), **extra}, account=key).connect()
        started = flow.start(client)
    except (RuntimeError, OSError) as exc:  # the server didn't start, or refused the call
        return {"error": f"OpenCode sign-in could not start: {exc}"}
    if not started.get("ok"):
        return {"error": str(started.get("error") or "OpenCode did not start the sign-in.")}
    if started.get("needs_code"):
        # ponytail: a sign-in that wants a code pasted back needs a text box the row lacks.
        return {"error": "This sign-in needs a code pasted back. Add that provider's API key under "
                         "Settings → LLMs instead; OpenCode uses it automatically."}
    url, code = str(started.get("auth_url") or ""), str(started.get("user_code") or "")
    if url:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - the row still shows where to go
            pass
    with _login_lock:
        _logins[key] = flow
    message = "Sign in in the browser tab that opened…" if url else str(started.get("message") or "Waiting for sign-in…")
    return {"message": f"{message} Code: {code}" if code else message}


def _login_state(config_dir: str | None) -> dict[str, Any]:
    """{pending[, installing]} · {error} · {} from the install and this account's sign-in."""
    from . import opencode_install as install

    if install.busy():
        return {"pending": True, "installing": True}
    key = str(config_dir or "")
    with _login_lock:
        flow = _logins.get(key)
    if flow is None:
        return {}
    try:
        state = flow.status()
    except (RuntimeError, OSError) as exc:
        state = {"error": f"Sign-in stopped: {exc}"}
    if state.get("needs_login"):
        return {"pending": True}
    with _login_lock:
        _logins.pop(key, None)
    return {"error": str(state["error"])} if state.get("error") else {}


def account_status(config_dir: str | None) -> dict[str, Any]:
    state = _login_state(config_dir)
    if state.get("pending"):
        return {"loggedIn": False, **state}
    try:
        data = json.loads(auth_path(config_dir).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    names = sorted(k for k, v in data.items() if isinstance(v, dict)) if isinstance(data, dict) else []
    if names:
        return {"loggedIn": True, "plan": ", ".join(names)}
    return {"loggedIn": False, **state}


def account_logout(config_dir: str | None) -> None:
    try:
        auth_path(config_dir).unlink()
    except OSError:
        pass


# What OpenCode relays from its providers when a key or plan is spent.
_LIMIT_RE = re.compile(
    r"\b429\b|rate[_ ]limit|too many requests|insufficient[_ ]quota|quota exceeded"
    r"|resource[_ ]exhausted|usage limit|exceeded your current quota",
    re.I,
)


def is_limit_error(text: str) -> bool:
    return isinstance(text, str) and bool(_LIMIT_RE.search(text))


# OpenCode providerID (``provider/model``) → Ducky secret id.
MODEL_SECRET = {"anthropic": "anthropic", "openai": "openai", "google": "gemini", "moonshotai": "kimi",
                "xai": "spacexai", "openrouter": "openrouter", "opencode": "opencode"}


def note_key_limit(model: str, *texts: Any) -> str:
    """A turn hit a quota: the model's provider key is marked so the next turn uses the next one.

    Returns the secret id marked ("" when nothing was).
    """
    secret = MODEL_SECRET.get((model or "").split("/", 1)[0], "opencode")
    if not any(is_limit_error(t) for t in texts if t):
        return ""
    acc, key = picked_key(secret)
    if not (acc and key):
        return ""
    try:
        from backend.agent.accounts import mark_exhausted

        mark_exhausted(secret, acc)
    except Exception:
        return ""
    return secret


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
