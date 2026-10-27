"""A full Ducky turn against a fake server that replays real OpenCode transcripts."""

import json
import queue
import threading

import pytest


class FakeHub:
    def __init__(self):
        self.queues = {}

    def subscribe(self, sid, timeout=10.0):
        self.queues[sid] = queue.Queue()
        return self.queues[sid]

    def unsubscribe(self, sid):
        self.queues.pop(sid, None)


class FakeClient:
    def __init__(self, hub, lines, *, known=("ses_old",)):
        self.hub, self.lines, self.known = hub, lines, set(known)
        self.calls = []
        self.replies = []

    def put_mcp(self, name, directory, config):
        self.calls.append(("put_mcp", name, config["command"]))

    def wait_mcp(self, name, directory, timeout=20.0):
        return "connected"

    def get_session(self, sid):
        return {"id": sid} if sid in self.known else None

    def create_session(self, directory, *, agent="", model=None, permissions=None):
        self.calls.append(("create", agent, model, permissions))
        return "ses_new"

    def update_session(self, sid, *, permissions):
        self.calls.append(("update", sid, permissions))

    def switch_agent(self, sid, agent):
        self.calls.append(("agent", sid, agent))

    def switch_model(self, sid, model):
        self.calls.append(("model", sid, model))

    def prompt(self, sid, text, files=None):
        self.calls.append(("prompt", sid, text, files))
        q = self.hub.queues[sid]
        for line in self.lines:
            event = json.loads(line)
            event["data"]["sessionID"] = sid
            q.put(event)

    def reply_permission(self, sid, rid, decision, message=""):
        self.replies.append((rid, decision))

    def interrupt(self, sid):
        self.calls.append(("interrupt", sid))
        self.hub.queues[sid].put({"type": "session.execution.interrupted", "data": {"sessionID": sid, "reason": "user"}})
        return True


@pytest.fixture
def turn(oc, fixture_lines, tmp_path, monkeypatch):
    ad = oc("opencode_adapter")
    monkeypatch.setattr(ad, "approval_mode", lambda conv: "ask")
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"uefn": {"command": "bridge", "args": ["--ducky-run-id", "r9"]}}}))

    def run(name, *, session_id="", mode="agent", model="auto", cancel=None, lines=None):
        hub = FakeHub()
        client = FakeClient(hub, lines if lines is not None else fixture_lines(name))
        events = []
        result = ad.OpenCodeAdapter()._turn(
            client, hub, prompt="do it", system_prompt="SYSTEM", cwd=str(tmp_path), conv_id="conv-1",
            model=model, mcp_config_path=str(mcp), env={"DUCKY_THINKING_EFFORT": "high"}, push=events.append,
            session_id=session_id, run_id="r9", cancel=cancel, timeout_s=0, image_paths=[], mode=mode,
            status=lambda t: events.append({"type": "status", "text": t}), alive=lambda: True)
        return result, client, events

    run.ad = ad
    return run


def test_new_session_turn(turn):
    result, client, events = turn("simple")
    assert result.ok and result.status == "done" and result.effective_mode == "agent"
    assert result.reply_text == "hi there" and result.upstream_session_id == "ses_new"
    assert result.usage["output_tokens"] == 25
    create = next(c for c in client.calls if c[0] == "create")
    assert create[1] == "build" and create[2] is None  # "auto" lets OpenCode pick; nothing is sent
    assert create[3][1]["action"] == "ducky-conv1*"
    assert client.calls[0] == ("put_mcp", "ducky-conv1", ["bridge", "--ducky-run-id", "r9"])
    prompt = next(c for c in client.calls if c[0] == "prompt")
    assert prompt[2].startswith("SYSTEM\n\n")  # system prompt on the first turn only


def test_resume_reapplies_rules_agent_and_model(turn):
    result, client, _ = turn("multi_turn", session_id="ses_old", model="anthropic/claude-sonnet-5-5")
    assert result.ok and result.upstream_session_id == "ses_old"
    kinds = [c[0] for c in client.calls]
    assert "create" not in kinds and kinds[1:4] == ["update", "agent", "model"]
    assert client.calls[3][2] == {"providerID": "anthropic", "id": "claude-sonnet-5-5", "variant": "high"}
    assert not next(c for c in client.calls if c[0] == "prompt")[2].startswith("SYSTEM")


def test_stale_session_starts_fresh(turn):
    result, client, _ = turn("simple", session_id="ses_gone")
    assert result.ok and result.upstream_session_id == "ses_new"


def test_plan_mode_uses_the_plan_agent(turn):
    result, client, _ = turn("simple", mode="plan")
    assert result.ok and result.effective_mode == "plan"
    create = next(c for c in client.calls if c[0] == "create")
    assert create[1] == "plan"
    assert {"action": "edit", "resource": "*", "effect": "deny"} in create[3]


@pytest.mark.parametrize("answer,ok", [("once", True), ("reject", True)])
def test_permission_goes_to_ducky_card_and_never_always(turn, monkeypatch, answer, ok):
    asked = []
    monkeypatch.setattr(turn.ad, "decide_permission", lambda req, conv, cwd: asked.append(req) or (answer, ""))
    fixture = "tool_approval" if answer == "once" else "tool_reject"
    result, client, _ = turn(fixture)
    assert asked and asked[0]["action"] == "edit"
    assert [d for _rid, d in client.replies] == [answer]
    assert result.ok is ok
    if answer == "reject":
        assert result.reply_text.endswith("Stopped: you declined write.")


def test_provider_failure_is_an_error_with_its_message(turn):
    result, _client, _ = turn("mcp_two_chats")
    assert not result.ok and result.status == "error" and "Endpoint is unavailable" in result.error
    assert result.effective_mode == ""


def test_stop_interrupts_the_session(turn, fixture_lines):
    cancel = threading.Event()
    cancel.set()
    started = [fixture_lines("interrupt")[2]]  # session.execution.started only; the turn runs on
    result, client, _ = turn("interrupt", cancel=cancel, lines=started)
    assert ("interrupt", "ses_new") in client.calls
    assert not result.ok and result.status == "cancelled"


def test_register_adds_picker_row_agent_login_and_hookup(oc, monkeypatch):
    pkg = __import__("_oc")
    inst = oc("opencode_install")
    monkeypatch.setattr(inst, "schedule_install_on_load", lambda: None)
    calls = {}

    class Api:
        def register_llm_provider(self, pid, **kw):
            calls["provider"] = (pid, kw)

        def register_coding_agent(self, aid, **kw):
            calls["agent"] = (aid, kw)

        def register_ide_hookup(self, kind, **kw):
            calls["ide"] = kind

        def log(self, _msg):
            pass

    pkg.register(Api())
    assert calls["provider"][0] == "opencode" and calls["provider"][1]["key_optional"] is True
    aid, kw = calls["agent"]
    assert aid == "opencode" and callable(kw["login"]) and callable(kw["login_status"])
    # "default" would make Ducky core hold the message for a model pick.
    assert kw["normalize_model"]("") == kw["normalize_model"]("default") == "auto"
    assert kw["normalize_model"]("anthropic/x") == "anthropic/x"
    assert calls["ide"] == "opencode"
    assert type(kw["factory"]()).__name__ == "OpenCodeAdapter"


def test_detect_never_starts_a_server(oc, monkeypatch):
    ad = oc("opencode_adapter")
    inst = oc("opencode_install")
    monkeypatch.setattr(inst, "resolve_bin", lambda override="": "")
    monkeypatch.setattr(ad, "shared_server", lambda *a, **k: pytest.fail("detect started the server"))
    info = ad.OpenCodeAdapter().detect(type("S", (), {"coding_agents": {}})())
    assert info.available and "first message" in info.status
    assert info.models[0]["id"] == "auto"
