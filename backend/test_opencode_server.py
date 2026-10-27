"""Server owner and event hub against a tiny stand-in OpenCode (no network, no real binary)."""

import os
import stat
import sys
import threading
import time
from pathlib import Path

FAKE = r'''
import base64, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PW = os.environ.get("OPENCODE_PASSWORD", "")
OK = "Basic " + base64.b64encode(("opencode:" + PW).encode()).decode()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.headers.get("Authorization") != OK or os.environ.get("OPENCODE_SERVER_PASSWORD"):
            self.send_response(401); self.end_headers(); return
        if self.path.startswith("/api/info"):
            body = json.dumps({"version": "2.2.0", "keys": sorted(k for k in os.environ if k.endswith("_API_KEY"))}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/api/event"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"type":"server.connected","data":{}}\n\n'); self.wfile.flush()
            time.sleep(0.3)
            for sid in ("ses_a", "ses_b", "ses_a", "ses_zz"):
                self.wfile.write(("data: " + json.dumps({"type": "session.text.delta", "data": {"sessionID": sid, "delta": sid}}) + "\n\n").encode())
                self.wfile.flush()
            time.sleep(2)
            return
        self.send_response(404); self.end_headers()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
print("server listening on http://127.0.0.1:%d" % srv.server_address[1], flush=True)
srv.serve_forever()
'''


def _fake_binary(tmp_path: Path) -> str:
    script = tmp_path / "fake_opencode.py"
    script.write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "opencode.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "opencode"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


def test_server_starts_with_private_password_and_keys(oc, tmp_path, monkeypatch):
    srv_mod = oc("opencode_server")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "inherited")  # must never reach the server
    server = srv_mod.OpenCodeServer(_fake_binary(tmp_path), lambda: {"ANTHROPIC_API_KEY": "k"})
    try:
        client, hub = server.connect()
        assert client.base_url.startswith("http://127.0.0.1:")
        assert client.info()["keys"] == ["ANTHROPIC_API_KEY"]
        again, _ = server.connect()
        assert again is client  # one server for every chat
    finally:
        server.stop()
    assert not server.alive()


def test_one_reader_routes_events_to_their_session(oc, tmp_path):
    srv_mod = oc("opencode_server")
    server = srv_mod.OpenCodeServer(_fake_binary(tmp_path), dict)
    try:
        _client, hub = server.connect()
        before = threading.active_count()
        queues = {sid: hub.subscribe(sid) for sid in ("ses_a", "ses_b", "ses_c", "ses_d", "ses_e")}
        assert threading.active_count() - before == 1  # five chats, one reader
        got = {sid: [] for sid in queues}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (len(got["ses_a"]) < 2 or not got["ses_b"]):
            for sid, q in queues.items():
                while not q.empty():
                    got[sid].append(q.get()["data"]["delta"])
            time.sleep(0.05)
        assert got["ses_a"] == ["ses_a", "ses_a"] and got["ses_b"] == ["ses_b"]
        assert got["ses_c"] == [] and got["ses_e"] == []
    finally:
        server.stop()
