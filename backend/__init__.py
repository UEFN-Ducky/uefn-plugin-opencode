"""OpenCode gateway: the OpenCode coding agent in Ducky, ready out of the box."""

from __future__ import annotations

from typing import Any

from . import opencode_auth as _auth
from .opencode_auth import LoginFlow

_INSTALL_HELP = (
    "Nothing to install: Ducky downloads its own OpenCode (checked against a pinned hash) on the "
    "first message. It uses the API keys you saved in Ducky, or OpenCode's free model, and Log in "
    "signs in a subscription (ChatGPT, GitHub Copilot, SuperGrok)."
)
_flow = LoginFlow()


def _normalize_model(model: str) -> str:
    # Core treats "default" as no model (the message would wait for a pick); "auto" lets
    # OpenCode choose: the person's keys, else its free model.
    mid = (model or "").strip()
    return mid if mid and mid.lower() not in ("auto", "default") else "auto"


def _test_key(api_key: str) -> dict[str, Any]:
    key = (api_key or "").strip()
    if key and len(key) < 8:
        return {"ok": False, "detail": "Key looks too short"}
    return {"ok": True, "detail": "OK" if key else "No key needed: Ducky's keys or the free model"}


def _model_infos() -> list[Any]:
    """Picker rows from the disk cache the running server last wrote (no process, no HTTP)."""
    from dataclasses import fields

    from backend.agent.model_fetch import ModelInfo

    from .opencode_adapter import cached_models

    names = {f.name for f in fields(ModelInfo)}
    out = []
    for row in cached_models():
        kw = {"id": row["id"], "display_name": row.get("name") or row["id"], "supports_tools": True,
              "supports_thinking_effort": bool(row.get("supports_thinking_effort")),
              "thinking_menu": row.get("thinking_menu")}
        out.append(ModelInfo(**{k: v for k, v in kw.items() if k in names and v is not None}))
    return out


def _provider_factory(api_key: str, model: str, **_kw: Any) -> Any:
    """OpenCode runs as a coding agent; the embedded Ducky agent never streams through it."""

    class _Stub:
        async def stream_turn(self, **_kwargs: Any):
            raise RuntimeError("OpenCode runs as the OpenCode coding agent: pick OpenCode in the model picker.")
            yield  # async-gen marker (unreachable)

        async def test_connection(self) -> tuple[bool, str]:
            res = _test_key(api_key)
            return bool(res["ok"]), str(res["detail"])

    return _Stub()


def _client(cli_path: str = "") -> Any:
    from . import opencode_install as install
    from .opencode_auth import server_env
    from .opencode_server import shared_server

    binary = install.resolve_bin(cli_path)
    if not binary:
        done = install.install()
        if not done.get("ok"):
            raise RuntimeError(str(done.get("error") or "Could not install OpenCode"))
        binary = str(done["cli_path"])
    client, _hub = shared_server(binary, server_env).connect()
    return client


def login(*, cli_path: str = "", **_kw: Any) -> dict[str, Any]:
    return _flow.start(_client(cli_path))


def login_status(**_kw: Any) -> dict[str, Any]:
    return _flow.status()


def login_submit(*, code: str = "", **_kw: Any) -> dict[str, Any]:
    return _flow.submit(code)


def register(api) -> None:
    from .opencode_adapter import OpenCodeAdapter
    from .opencode_install import schedule_install_on_load

    # The picker lists gateways by provider row; the key is optional (OPENCODE_API_KEY).
    api.register_llm_provider(
        "opencode",
        factory=_provider_factory,
        fetch_models=lambda api_key, **_kw: _model_infos(),
        read_cached_models=_model_infos,
        test_key=_test_key,
        key_optional=True,
        shows_thinking_effort=True,
        is_quota_error=_auth.is_limit_error,
    )
    api.register_coding_agent(
        "opencode",
        factory=lambda: OpenCodeAdapter(),
        aliases=["open_code", "opencode_cli"],
        normalize_model=_normalize_model,
        settings_defaults={"enabled": True, "cli_path": "", "default_args": ""},
        install_help=_INSTALL_HELP,
        test_key=_test_key,
        token_provider="opencode",
        shows_thinking_effort=True,
        login=login,
        login_status=login_status,
        login_submit=login_submit,
        account_env=_auth.account_env,
        account_login=_auth.account_login,
        account_status=_auth.account_status,
        account_logout=_auth.account_logout,
        is_limit_error=_auth.is_limit_error,
        # OpenCode enforces its own permissions; its requests become Ducky approval cards.
        chat_permission_modes=("ask", "edits", "all"),
    )
    api.register_ide_hookup("opencode", label="OpenCode")
    schedule_install_on_load()
    api.log("OpenCode gateway active (coding agent + IDE hookup)")
