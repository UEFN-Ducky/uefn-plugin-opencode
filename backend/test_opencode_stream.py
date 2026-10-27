"""Replays of real OpenCode 2.2 transcripts (backend/fixtures, scripts/record_fixture.py)."""

import json


def _run(oc, fixture_lines, name):
    events = []
    stream = oc("opencode_stream").replay(fixture_lines(name), events.append)
    return stream, events


def test_simple_reply_and_usage(oc, fixture_lines):
    stream, events = _run(oc, fixture_lines, "simple")
    assert stream.outcome == "succeeded"
    assert stream.trailing_text() == "hi there"
    assert [b["type"] for b in stream.blocks] == ["thinking"]
    assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "hi there"
    assert stream.usage["input_tokens"] == 15349 and stream.usage["output_tokens"] == 25
    assert stream.usage["cache_read_tokens"] == 1408 and stream.usage["context_tokens"] == 15349 + 1408
    assert stream.usage["cumulative_thread"].startswith("ses_")


def test_second_turn_on_the_same_session(oc, fixture_lines):
    stream, _events = _run(oc, fixture_lines, "multi_turn")
    assert stream.outcome == "succeeded" and "a.txt" in stream.all_text()


def test_approved_edit_is_one_tool_row(oc, fixture_lines):
    stream, events = _run(oc, fixture_lines, "tool_approval")
    assert stream.outcome == "succeeded"
    tools = [b for b in stream.blocks if b["type"] == "tool_call"]
    assert [(t["name"], t["status"]) for t in tools] == [("write", "success")]
    assert tools[0]["arguments"]["content"] == "duck"
    assert tools[0]["result"]["data"] == "Created file successfully: a.txt"
    kinds = [e["type"] for e in events if e["type"] in ("tool", "tool_done")]
    assert kinds == ["tool", "tool_done"]  # the UI pairs them by position


def test_rejected_edit_ends_the_turn_as_declined(oc, fixture_lines):
    stream, _events = _run(oc, fixture_lines, "tool_reject")
    assert stream.outcome == "interrupted" and stream.interrupt_reason == "shutdown"
    assert stream.declined == ["write"]
    assert [b["status"] for b in stream.blocks if b["type"] == "tool_call"] == ["error"]


def test_stop_is_an_interrupt_by_the_user(oc, fixture_lines):
    stream, _events = _run(oc, fixture_lines, "interrupt")
    assert stream.outcome == "interrupted" and stream.interrupt_reason == "user"


def test_provider_failure_after_retries(oc, fixture_lines):
    stream, events = _run(oc, fixture_lines, "mcp_two_chats")
    assert stream.outcome == "failed" and stream.error_status == "error"
    assert "Endpoint is unavailable" in stream.error_text
    assert any("retrying" in e["text"] for e in events if e["type"] == "status")
    # Code mode: Ducky's MCP tools run inside one `execute` tool.
    assert [b["name"] for b in stream.blocks if b["type"] == "tool_call"] == ["execute"]


def test_long_reply_is_batched(oc):
    mod = oc("opencode_stream")
    now = [0.0]
    events = []
    stream = mod.OpenCodeStream("c", "r", events.append, clock=lambda: now[0])
    for i in range(10_000):  # 10k tokens, 1 ms apart
        now[0] += 0.001
        stream.on_event({"type": "session.text.delta", "data": {"delta": "x"}})
    stream.flush(force=True)
    deltas = [e for e in events if e["type"] == "text_delta"]
    assert "".join(e["text"] for e in deltas) == "x" * 10_000
    assert len(deltas) < 300


def test_tool_result_keeps_a_bounded_tail(oc):
    mod = oc("opencode_stream")
    events = []
    stream = mod.OpenCodeStream("c", "r", events.append)
    stream.on_event({"type": "session.tool.input.started", "data": {"id": "t", "name": "ducky-abc_ducky_get_status"}})
    stream.on_event({"type": "session.tool.called", "data": {"id": "t", "input": {}}})
    stream.on_event({"type": "session.tool.success",
                     "data": {"id": "t", "content": [{"type": "text", "text": "y" * 50_000 + "END"}]}})
    block = stream.blocks[-1]
    assert block["name"] == "ducky_get_status"
    assert len(block["result"]["data"]) <= mod.RESULT_CHARS + 20 and block["result"]["data"].endswith("END")


def test_error_classes(oc):
    classify = oc("opencode_stream").classify_error
    assert classify({"status": 401, "message": "bad"})[0] == "needs_login"
    assert classify({"message": "Invalid API key"})[0] == "needs_login"
    assert classify({"status": 429, "message": "slow down"})[0] == "usage_limit"
    assert classify({"status": 503, "message": "Endpoint is unavailable."})[0] == "error"


def test_fixtures_hold_no_personal_paths(fixture_lines):
    for name in ("simple", "multi_turn", "tool_approval", "tool_reject", "interrupt", "mcp_two_chats"):
        text = "\n".join(fixture_lines(name))
        assert "Users\\\\" not in text and "AppData" not in text, name
        for line in fixture_lines(name):
            json.loads(line)
