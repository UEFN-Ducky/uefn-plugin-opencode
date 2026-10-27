"""Record a real OpenCode 2 event transcript for replay tests.

Usage (against a running `opencode serve`):
  py -3 scripts/record_fixture.py --url http://127.0.0.1:4096 --password PW \
      --dir C:/some/project --out backend/fixtures/simple.jsonl --prompt "say hi"

Scenarios: --interrupt-after S stops the turn S seconds after sending;
--reply once|reject answers every permission request; --session reuses a session;
--rules '<json ruleset>' sets session permissions; --mcp name=<json config> adds MCP.
Each output line is one event as the server sent it (JSON).
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

END_TYPES = {"session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"}


def _req(url: str, pw: str, method: str = "GET", body: object = None, timeout: float = 30.0) -> object:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _session_of(ev: dict) -> str:
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    return str(data.get("sessionID") or ev.get("sessionID") or "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--session", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--agent", default="")
    ap.add_argument("--rules", default="")
    ap.add_argument("--mcp", action="append", default=[])
    ap.add_argument("--reply", default="")
    ap.add_argument("--interrupt-after", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=240.0)
    a = ap.parse_args()
    base = a.url.rstrip("/")
    loc = {"directory": a.dir}

    events: list[dict] = []
    done = threading.Event()
    sid_box = {"sid": a.session}

    def reader() -> None:
        req = urllib.request.Request(base + "/api/event", headers={
            "Accept": "text/event-stream",
            "Authorization": "Basic " + base64.b64encode(f"opencode:{a.password}".encode()).decode(),
        })
        with urllib.request.urlopen(req, timeout=a.timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                sid = sid_box["sid"]
                if not sid or _session_of(ev) not in ("", sid):
                    continue
                if not _session_of(ev):
                    continue
                events.append(ev)
                et = str(ev.get("type") or "")
                data = ev.get("data") or {}
                rid = str(data.get("requestID") or data.get("id") or "")
                if a.reply and et == "permission.asked" and rid.startswith("per"):
                    try:
                        _req(f"{base}/api/session/{sid}/permission/{rid}/reply", a.password, "POST", {"decision": a.reply})
                    except OSError as exc:
                        print(f"reply failed: {exc}")
                if et in END_TYPES:
                    done.set()
                    return

    for spec in a.mcp:
        name, cfg = spec.split("=", 1)
        _req(f"{base}/api/experimental/mcp/{name}?location[directory]={urllib.parse.quote(a.dir)}",
             a.password, "PUT", {"config": json.loads(cfg)})

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.5)
    if not sid_box["sid"]:
        body: dict = {"location": loc}
        if a.model:
            prov, mid = a.model.split("/", 1)
            body["model"] = {"providerID": prov, "id": mid}
        if a.agent:
            body["agent"] = a.agent
        if a.rules:
            body["permissions"] = json.loads(a.rules)
        sid_box["sid"] = _req(base + "/api/session", a.password, "POST", body)["data"]["id"]
    sid = sid_box["sid"]
    _req(f"{base}/api/session/{sid}/prompt", a.password, "POST", {"text": a.prompt})
    if a.interrupt_after:
        time.sleep(a.interrupt_after)
        _req(f"{base}/api/session/{sid}/interrupt", a.password, "POST", None)
    done.wait(a.timeout)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    print(json.dumps({"session": sid, "events": len(events), "ended": done.is_set(),
                      "types": sorted({str(e.get("type")) for e in events})}))


if __name__ == "__main__":
    main()
