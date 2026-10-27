"""OpenCode 2 HTTP API and its one shared event stream (stdlib only).

The server speaks HTTP Basic with user ``opencode`` on ``/api/*``. Events come from
``GET /api/event`` as server-sent events; one reader thread per server routes them to
the chat that owns the session, so ten chats cost one connection, not ten.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Only what a Ducky turn reads. The server filters, so token-heavy types we ignore never cross the wire.
EVENT_TYPES = (
    "session.execution.started",
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "session.text.delta",
    "session.text.ended",
    "session.reasoning.delta",
    "session.reasoning.ended",
    "session.tool.input.started",
    "session.tool.called",
    "session.tool.progress",
    "session.tool.success",
    "session.tool.failed",
    "session.step.ended",
    "session.step.failed",
    "session.usage.updated",
    "session.retry.scheduled",
    "permission.asked",
)
# Heartbeats arrive every 10-15 s; longer silence means a dead connection, not an idle one.
SILENCE_S = 45.0


class OpenCodeError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _auth(password: str) -> str:
    # OpenCode wants UTF-8 Basic credentials.
    return "Basic " + base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")


def _location(directory: str) -> dict[str, str]:
    return {"location[directory]": directory} if directory else {}


class OpenCodeClient:
    def __init__(self, base_url: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._authorization = _auth(password)

    def request(self, method: str, path: str, body: Any = None, *,
                query: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._authorization)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read() or b"{}").get("message") or "")
            except (ValueError, AttributeError, OSError):
                pass
            where = f"{method} {path}"
            raise OpenCodeError(exc.code, f"{where}: {detail or f'HTTP {exc.code}'}") from None
        return json.loads(raw) if raw else None

    def info(self, timeout: float = 5.0) -> dict[str, Any]:
        return self.request("GET", "/api/info", timeout=timeout) or {}

    def create_session(self, directory: str, *, agent: str = "", model: dict | None = None,
                       permissions: list[dict] | None = None) -> str:
        body: dict[str, Any] = {"location": {"directory": directory}}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        if permissions is not None:
            body["permissions"] = permissions
        return str(self.request("POST", "/api/session", body)["data"]["id"])

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            return (self.request("GET", f"/api/session/{session_id}") or {}).get("data")
        except OpenCodeError as exc:
            if exc.status in (400, 404):
                return None
            raise

    def update_session(self, session_id: str, *, permissions: list[dict]) -> None:
        self.request("PATCH", f"/api/session/{session_id}", {"permissions": permissions})

    def switch_agent(self, session_id: str, agent: str) -> None:
        self.request("POST", f"/api/session/{session_id}/agent", {"agent": agent})

    def switch_model(self, session_id: str, model: dict[str, str]) -> None:
        self.request("POST", f"/api/session/{session_id}/model", {"model": model})

    def prompt(self, session_id: str, text: str, files: list[dict] | None = None) -> None:
        body: dict[str, Any] = {"text": text}
        if files:
            body["files"] = files
        self.request("POST", f"/api/session/{session_id}/prompt", body)

    def interrupt(self, session_id: str) -> bool:
        out = self.request("POST", f"/api/session/{session_id}/interrupt", timeout=10.0) or {}
        return bool(out.get("interrupted"))

    def reply_permission(self, session_id: str, request_id: str, decision: str, message: str = "") -> None:
        # Never "always": OpenCode stores that for the whole project, wider than one Ducky chat.
        if decision not in ("once", "reject"):
            raise ValueError("decision must be once or reject")
        body: dict[str, Any] = {"decision": decision}
        if message:
            body["message"] = message
        self.request("POST", f"/api/session/{session_id}/permission/{request_id}/reply", body)

    def put_mcp(self, name: str, directory: str, config: dict[str, Any]) -> None:
        self.request("PUT", f"/api/experimental/mcp/{urllib.parse.quote(name)}", {"config": config},
                     query=_location(directory))

    def mcp_status(self, name: str, directory: str) -> str:
        """``connected`` / ``failed`` / … for one MCP entry in a folder ("" when not listed)."""
        rows = (self.request("GET", "/api/mcp", query=_location(directory)) or {}).get("data") or []
        for row in rows:
            if isinstance(row, dict) and row.get("name") == name:
                status = row.get("status")
                return str(status.get("status") if isinstance(status, dict) else status or "")
        return ""

    def wait_mcp(self, name: str, directory: str, timeout: float = 20.0) -> str:
        """Block until the entry connected or failed (tools added mid-turn arrive too late)."""
        deadline = time.monotonic() + timeout
        status = ""
        while time.monotonic() < deadline:
            status = self.mcp_status(name, directory)
            if status in ("connected", "failed", "disabled"):
                return status
            time.sleep(0.2)
        return status or "timeout"

    def models(self, directory: str = "") -> list[dict[str, Any]]:
        return list((self.request("GET", "/api/model", query=_location(directory)) or {}).get("data") or [])

    def default_model(self, directory: str = "") -> dict[str, Any]:
        return dict((self.request("GET", "/api/model/default", query=_location(directory)) or {}).get("data") or {})

    def integrations(self) -> list[dict[str, Any]]:
        return list((self.request("GET", "/api/integration") or {}).get("data") or [])

    def connect_key(self, integration: str, key: str) -> None:
        self.request("POST", f"/api/integration/{urllib.parse.quote(integration)}/connect/key", {"key": key})

    def oauth_start(self, integration: str, method_id: str) -> dict[str, Any]:
        out = self.request("POST", f"/api/integration/{urllib.parse.quote(integration)}/connect/oauth",
                           {"methodID": method_id})
        return dict((out or {}).get("data") or {})

    def oauth_status(self, integration: str, attempt: str) -> dict[str, Any]:
        out = self.request("GET", f"/api/integration/{urllib.parse.quote(integration)}/connect/oauth/{attempt}")
        return dict((out or {}).get("data") or {})

    def oauth_complete(self, integration: str, attempt: str, code: str = "") -> None:
        self.request("POST", f"/api/integration/{urllib.parse.quote(integration)}/connect/oauth/{attempt}/complete",
                     {"code": code} if code else {})


class EventHub:
    """One SSE reader per server, fanned out to per-session queues."""

    def __init__(self, client: OpenCodeClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue] = {}
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def subscribe(self, session_id: str, timeout: float = 10.0) -> queue.Queue:
        """Queue for one session; returns once the stream is live (events are not replayed)."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[session_id] = q
            if self._thread is None or not self._thread.is_alive():
                self._connected.clear()
                self._thread = threading.Thread(target=self._run, name="opencode-events", daemon=True)
                self._thread.start()
        self._connected.wait(timeout)
        return q

    def unsubscribe(self, session_id: str) -> None:
        with self._lock:
            self._queues.pop(session_id, None)

    def close(self) -> None:
        self._stop.set()

    def _route(self, event: dict[str, Any]) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        sid = str(data.get("sessionID") or "")
        with self._lock:
            q = self._queues.get(sid)
        if q is not None:
            q.put(event)

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            query = urllib.parse.urlencode({"types": list(EVENT_TYPES)}, doseq=True)
            req = urllib.request.Request(
                self._client.base_url + "/api/event?" + query,
                headers={"Accept": "text/event-stream", "Authorization": self._client._authorization},
            )
            try:
                with urllib.request.urlopen(req, timeout=SILENCE_S) as resp:
                    backoff = 0.5
                    self._connected.set()
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            self._route(event)
            except (OSError, urllib.error.URLError, TimeoutError):
                pass
            with self._lock:
                idle = not self._queues
            if idle:
                # ponytail: the reader ends when nobody listens; the next subscribe starts it again.
                with self._lock:
                    self._thread = None
                return
            self._connected.clear()
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
