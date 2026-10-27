"""OpenCode coding agent: Ducky chats run as sessions on one shared OpenCode 2 server.

Per turn: the chat's own ``ducky-<chat>`` MCP entry (its run identity), session permission
rules (only its own Ducky tools; Ducky's approval mode), the native plan agent for Ask and
Plan, then one prompt whose events stream back through the shared event hub. OpenCode
enforces permissions; its requests become Ducky approval cards, always answered "once".
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from backend.agent.coding_agents import base as _base
from backend.agent.coding_agents.base import CodingAgentCapabilities, CodingAgentInfo, CodingAgentLaunchResult
from backend.agent.coding_agents.settings_helpers import coding_agent_cfg

from . import opencode_install as install
from .opencode_auth import model_rows, server_env
from .opencode_client import OpenCodeError
from .opencode_config import agent_for_mode, chat_tag, mcp_entry, model_ref, permission_card, session_rules
from .opencode_server import ServerStartError, current_server, shared_server
from .opencode_stream import FLUSH_S, OpenCodeStream

STOP_GRACE_S = 15.0
EARLY_STOP_S = 2.0
DEFAULT_MODEL = {"id": "default", "name": "Default (your keys, else OpenCode's free model)",
                 "provider": "OpenCode", "supports_tools": True}
# conv id → (client, session id) of the running turn, for request_stop (adapters are rebuilt per call).
_running: dict[str, tuple[Any, str]] = {}
_running_lock = threading.Lock()
_models_lock = threading.Lock()


def models_cache_path() -> Path:
    return install.tools_root() / "models.json"


def cached_models() -> list[dict[str, Any]]:
    try:
        rows = json.loads(models_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    return [DEFAULT_MODEL, *[r for r in rows if isinstance(r, dict) and r.get("id")]]


def refresh_models_async(client: Any, directory: str) -> None:
    """Model list straight from the running server, cached for the picker (never blocks a turn)."""
    if not _models_lock.acquire(blocking=False):
        return

    def work() -> None:
        try:
            rows = model_rows(client.models(directory))
            if rows:
                path = models_cache_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rows), encoding="utf-8")
                _base.invalidate_detect_cache()
        except Exception:
            pass
        finally:
            _models_lock.release()

    threading.Thread(target=work, name="opencode-models", daemon=True).start()


def approval_mode(conv_id: str) -> str:
    try:
        from backend.tools.panel.permission_prompt import permission_mode

        return permission_mode(conv_id)
    except Exception:
        return "ask"


def decide_permission(request: dict[str, Any], conv_id: str, cwd: str) -> tuple[str, str]:
    """("once"|"reject", message) from Ducky's approval card for one OpenCode request."""
    tool, tool_input = permission_card(request, cwd)
    try:
        from backend.tools.panel.permission_prompt import decide

        out = decide(tool, tool_input, conv_id=conv_id, project_root=cwd)
    except Exception as exc:  # noqa: BLE001 - no card means no approval
        return "reject", f"No approval: {exc}"
    if out.get("behavior") == "allow":
        return "once", ""
    return "reject", str(out.get("message") or "The user denied this.")


class OpenCodeAdapter:
    id = "opencode"
    label = "OpenCode"
    capabilities = CodingAgentCapabilities(
        terminal_agent=True, chat_api=False, a2a=True, mcp_inject=True,
        needs_api_key=False, needs_cli=False, resume=True,
        supported_modes=("agent", "ask", "plan"),
    )

    def detect(self, settings: Any) -> CodingAgentInfo:
        """Cheap: version only. Never starts the server, a session or a login."""
        cfg = coding_agent_cfg(settings, self.id)
        enabled = bool(cfg.get("enabled", True))
        override = str(cfg.get("cli_path") or "")
        path = install.resolve_bin(override)
        busy = install.status_text()
        if not enabled:
            status = "Disabled — turn on to use in chat"
        elif path:
            version = install.read_version(path)
            if version and not install.is_v2(version):
                status = f"OpenCode {version} found; Ducky needs OpenCode 2 (it installs its own on the next message)"
                path = ""
            else:
                status = f"OpenCode {version or install.OPENCODE_VERSION} · ready"
                server = current_server()
                status += " · running" if server is not None and server.alive() else ""
        elif busy:
            status = busy
        else:
            status = f"Ready · Ducky installs OpenCode {install.OPENCODE_VERSION} on the first message"
        info = CodingAgentInfo(
            id=self.id, label=self.label, enabled=enabled, available=enabled,
            status=status, cli_path=path or override, default_args=str(cfg.get("default_args") or ""),
            capabilities=self.capabilities, models=cached_models(),
        )
        info.logged_in = None  # type: ignore[attr-defined] - keys, sign-in or the free model all work
        return info

    def request_stop(self, conv_id: str) -> bool:
        with _running_lock:
            running = _running.get(conv_id)
        if running is None:
            return False
        client, session_id = running
        try:
            client.interrupt(session_id)
        except (OSError, OpenCodeError):
            return False
        return True

    def launch(self, *, prompt: str, system_prompt: str, cwd: str, conv_id: str, model: str,
               mcp_config_path: str, extra_args: str, cli_path: str, env: dict[str, str], push: Any,
               session_id: str = "", run_id: str = "", cancel: threading.Event | None = None,
               timeout_s: float = 0.0, image_paths: list[str] | None = None,
               mode: str = "agent") -> CodingAgentLaunchResult:
        try:
            mode = _base.normalize_coding_mode(mode)
        except ValueError as exc:
            return CodingAgentLaunchResult(ok=False, status="error", error=str(exc),
                                           upstream_session_id=session_id, requested_mode=str(mode))

        def fail(message: str, status: str = "error", sid: str = session_id) -> CodingAgentLaunchResult:
            return CodingAgentLaunchResult(ok=False, status=status, error=message, upstream_session_id=sid,
                                           requested_mode=mode, effective_mode="")

        def status(text: str) -> None:
            push({"type": "status", "text": text, "conv_id": conv_id, "run_id": run_id})

        binary = install.resolve_bin(cli_path)
        if not binary:
            status(f"Installing OpenCode {install.OPENCODE_VERSION} (one time)…")
            done = install.install(on_progress=status)
            if not done.get("ok"):
                return fail(str(done.get("error") or "Could not install OpenCode"))
            binary = str(done["cli_path"])

        server = shared_server(binary, server_env)
        server.begin_turn()
        sid = ""
        try:
            if not server.alive():
                status("Starting OpenCode…")
            try:
                client, hub = server.connect()
            except (ServerStartError, OSError) as exc:
                return fail(str(exc))
            refresh_models_async(client, cwd)
            try:
                return self._turn(client, hub, prompt=prompt, system_prompt=system_prompt, cwd=cwd,
                                  conv_id=conv_id, model=model, mcp_config_path=mcp_config_path, env=env,
                                  push=push, session_id=session_id, run_id=run_id, cancel=cancel,
                                  timeout_s=timeout_s, image_paths=image_paths or [], mode=mode,
                                  status=status, alive=server.alive)
            except (OpenCodeError, OSError) as exc:
                return fail(f"OpenCode: {exc}")
        finally:
            server.end_turn()

    def _turn(self, client: Any, hub: Any, *, prompt: str, system_prompt: str, cwd: str, conv_id: str,
              model: str, mcp_config_path: str, env: dict[str, str], push: Any, session_id: str,
              run_id: str, cancel: threading.Event | None, timeout_s: float, image_paths: list[str],
              mode: str, status: Any, alive: Any) -> CodingAgentLaunchResult:
        entry = mcp_entry(mcp_config_path, env)
        if entry is not None:
            try:
                client.put_mcp(chat_tag(conv_id), cwd, entry)
                if client.wait_mcp(chat_tag(conv_id), cwd) != "connected":
                    entry = None
            except (OpenCodeError, OSError):
                entry = None
        if entry is None:
            # T3's rule: a turn survives without the tool server, it just has fewer tools.
            status("Ducky's tools didn't connect; OpenCode continues with its own tools.")
        rules = session_rules(conv_id, mode=mode, approval=approval_mode(conv_id))
        agent = agent_for_mode(mode)
        effort = str((env or {}).get("DUCKY_THINKING_EFFORT") or "")
        ref = model_ref(model, effort)
        sid = session_id if session_id and client.get_session(session_id) else ""
        resumed = bool(sid)
        if resumed:
            client.update_session(sid, permissions=rules)  # re-applied on resume (T3 #21143)
            client.switch_agent(sid, agent)
            if ref:
                client.switch_model(sid, ref)
        else:
            sid = client.create_session(cwd, agent=agent, model=ref, permissions=rules)
        events = hub.subscribe(sid)
        with _running_lock:
            _running[conv_id] = (client, sid)
        stream = OpenCodeStream(conv_id, run_id, push)
        try:
            text = prompt
            if not resumed and system_prompt.strip():
                text = system_prompt.strip() + "\n\n" + prompt
            if mode != "agent":
                text = (f"Ducky {mode.title()} mode: inspect read-only; do not change project files. "
                        + ("Create the plan with ducky_create_plan, then stop. " if mode == "plan"
                           else "Answer the question with read-only tools. ") + "\n\n" + text)
            files = [{"uri": Path(p).resolve().as_uri(), "name": Path(p).name}
                     for p in image_paths if Path(p).is_file()]
            status("Resumed OpenCode session…" if resumed else "OpenCode is working…")
            client.prompt(sid, text, files)
            ended, timed_out, died = self._pump(client, sid, events, stream, conv_id=conv_id, cwd=cwd,
                                                cancel=cancel, timeout_s=timeout_s, alive=alive)
        finally:
            hub.unsubscribe(sid)
            with _running_lock:
                _running.pop(conv_id, None)
        cancelled = bool(cancel is not None and cancel.is_set())
        note = "Cancelled before the tool finished." if cancelled else "Turn ended before the tool reported a result."
        stream.finish_open_tools(note)
        blocks = stream.finalize_blocks()
        reply = stream.trailing_text() or ("" if blocks else stream.all_text())
        common = dict(upstream_session_id=sid, reply_text=reply, streamed=True, blocks=blocks,
                      usage=stream.usage, requested_mode=mode)
        if timed_out:
            return CodingAgentLaunchResult(ok=False, status="timeout", effective_mode="",
                                           error=f"OpenCode timed out after {int(timeout_s)}s", **common)
        if died:
            return CodingAgentLaunchResult(ok=False, status="error", effective_mode="",
                                           error="The OpenCode server stopped during the turn.", **common)
        if cancelled:
            return CodingAgentLaunchResult(ok=False, status="cancelled", effective_mode="", error="Cancelled", **common)
        if stream.outcome == "succeeded":
            return CodingAgentLaunchResult(ok=True, status="done", effective_mode=mode, **common)
        if stream.outcome == "interrupted" and stream.declined:
            # A denied approval ends OpenCode's turn; that is the person's answer, not a failure.
            common["reply_text"] = (reply + "\n\n" if reply else "") + f"Stopped: you declined {stream.declined[-1]}."
            return CodingAgentLaunchResult(ok=True, status="done", effective_mode=mode, **common)
        if stream.outcome == "failed":
            status_ = "needs_login" if stream.error_status == "needs_login" else "error"
            return CodingAgentLaunchResult(ok=False, status=status_, effective_mode="", error=stream.error_text, **common)
        reason = stream.interrupt_reason or "no end event"
        return CodingAgentLaunchResult(ok=False, status="error", effective_mode="",
                                       error=f"OpenCode stopped the turn ({reason}).", **common)

    def _pump(self, client: Any, sid: str, events: queue.Queue, stream: OpenCodeStream, *, conv_id: str,
              cwd: str, cancel: threading.Event | None, timeout_s: float, alive: Any) -> tuple[bool, bool, bool]:
        """Feed events until the turn ends: (ended, timed_out, server_died)."""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        stop_asked_at = cancel_seen = 0.0
        while True:
            try:
                event = events.get(timeout=FLUSH_S)
            except queue.Empty:
                event = None
                stream.flush()
            if event is not None:
                if event.get("type") == "permission.asked":
                    request = event.get("data") or {}
                    stream.flush(force=True)
                    decision, message = decide_permission(request, conv_id, cwd)
                    client.reply_permission(sid, str(request.get("id") or ""), decision, message)
                    continue
                if stream.on_event(event):
                    return True, False, False
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                # Stop sent before OpenCode registered the prompt can be ignored: wait for its
                # start (up to 2 s), then interrupt; give the turn 15 s to end on its own.
                cancel_seen = cancel_seen or now
                if not stop_asked_at and (stream.started or now - cancel_seen > EARLY_STOP_S):
                    stop_asked_at = now
                    try:
                        client.interrupt(sid)
                    except (OSError, OpenCodeError):
                        pass
                if stop_asked_at and now - stop_asked_at > STOP_GRACE_S:
                    return False, False, False
            if deadline is not None and now > deadline:
                try:
                    client.interrupt(sid)
                except (OSError, OpenCodeError):
                    pass
                return False, True, False
            if not alive():
                return False, False, True
