"""The one OpenCode server Ducky owns: started on the first turn, shared by every chat.

Bound to 127.0.0.1 on a free port, protected by a random password that only lives in
this process's memory. Stopped after 10 idle minutes, when Ducky's keys change while
idle, and when the app exits.
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable

from .opencode_client import EventHub, OpenCodeClient, OpenCodeError

IDLE_STOP_S = 600.0
START_TIMEOUT_S = 45.0
READY_TIMEOUT_S = 15.0
_LISTEN_RE = re.compile(r"listening on (http://127\.0\.0\.1:\d+)")


class ServerStartError(RuntimeError):
    pass


class OpenCodeServer:
    def __init__(self, binary: str, env_fn: Callable[[], dict[str, str]]) -> None:
        self.binary = binary
        self._env_fn = env_fn
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._client: OpenCodeClient | None = None
        self._hub: EventHub | None = None
        self._env_key = ""
        self._active = 0
        self._last_used = time.monotonic()
        self._tail: deque[str] = deque(maxlen=40)
        self._idle_timer: threading.Timer | None = None

    # -- lifecycle -----------------------------------------------------------------------
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def connect(self) -> tuple[OpenCodeClient, EventHub]:
        """Running server's client + event hub, starting it (or restarting a dead one) first."""
        with self._lock:
            env = self._env_fn()
            key = repr(sorted(env.items()))
            if self.alive() and key != self._env_key and self._active == 0:
                self._stop_locked()  # Ducky's keys changed: pick them up between turns
            if not self.alive():
                self._start_locked(env, key)
            assert self._client is not None and self._hub is not None
            return self._client, self._hub

    def _start_locked(self, extra_env: dict[str, str], key: str) -> None:
        password = secrets.token_urlsafe(32)
        env = dict(os.environ)
        env.pop("OPENCODE_SERVER_PASSWORD", None)  # OPENCODE_PASSWORD is the v2 name; never inherit one
        env.update(extra_env)
        env.update({"OPENCODE_PASSWORD": password, "OPENCODE_DISABLE_AUTOUPDATE": "1"})
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._tail.clear()
        proc = subprocess.Popen(
            [self.binary, "serve", "--hostname", "127.0.0.1", "--port", "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace", **kwargs,
        )
        url_box: dict[str, str] = {}
        found = threading.Event()

        def pump() -> None:
            # Drains the pipe for the server's lifetime so it never blocks on a full buffer.
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._tail.append(line[:300])
                m = _LISTEN_RE.search(line)
                if m and not found.is_set():
                    url_box["url"] = m.group(1)
                    found.set()
            found.set()

        threading.Thread(target=pump, name="opencode-server-log", daemon=True).start()
        found.wait(START_TIMEOUT_S)
        url = url_box.get("url", "")
        if not url:
            _kill_tree(proc)
            raise ServerStartError("OpenCode did not start: " + (self.output_tail() or "no output"))
        client = OpenCodeClient(url, password)
        deadline = time.monotonic() + READY_TIMEOUT_S
        last = ""
        while True:  # only a real info answer proves readiness
            try:
                client.info(timeout=5.0)
                break
            except OpenCodeError as exc:
                last = "the server refused Ducky's password" if exc.status == 401 else str(exc)
            except OSError as exc:
                last = str(exc)
            if time.monotonic() > deadline or proc.poll() is not None:
                _kill_tree(proc)
                raise ServerStartError(f"OpenCode started but did not answer ({last or 'timeout'})")
            time.sleep(0.25)
        self._proc, self._client, self._hub, self._env_key = proc, client, EventHub(client), key

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._hub is not None:
            self._hub.close()
        if self._proc is not None:
            _kill_tree(self._proc)
        self._proc = self._client = self._hub = None

    def output_tail(self) -> str:
        return "\n".join(list(self._tail)[-8:])

    # -- usage accounting (idle stop) ------------------------------------------------------
    def begin_turn(self) -> None:
        with self._lock:
            self._active += 1
            self._last_used = time.monotonic()

    def end_turn(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_used = time.monotonic()
            self._arm_idle_locked()

    def _arm_idle_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(IDLE_STOP_S + 1, self._idle_check)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            if self._active == 0 and time.monotonic() - self._last_used >= IDLE_STOP_S:
                self._stop_locked()


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


_server: OpenCodeServer | None = None
_server_lock = threading.Lock()


def shared_server(binary: str, env_fn: Callable[[], dict[str, str]]) -> OpenCodeServer:
    """The app-wide server for this binary (a new binary replaces the old server)."""
    global _server
    with _server_lock:
        if _server is not None and _server.binary != binary:
            _server.stop()
            _server = None
        if _server is None:
            _server = OpenCodeServer(binary, env_fn)
        return _server


def current_server() -> OpenCodeServer | None:
    return _server


@atexit.register
def _stop_on_exit() -> None:
    if _server is not None:
        _server.stop()
