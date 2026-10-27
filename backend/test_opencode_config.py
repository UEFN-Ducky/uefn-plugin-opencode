import json


def test_mcp_entry_keeps_run_identity(oc, tmp_path):
    cfg = oc("opencode_config")
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"uefn": {
        "command": "UEFN-Ducky.exe", "args": ["--mcp", "--ducky-run-id", "r1"], "env": {"DUCKY_CONV_ID": "c1"}}}}))
    entry = cfg.mcp_entry(str(path), {"DUCKY_REQUESTED_MODE": "plan", "PATH": "x", "DUCKY_CONV_ID": "other"})
    assert entry["type"] == "local"
    assert entry["command"] == ["UEFN-Ducky.exe", "--mcp", "--ducky-run-id", "r1"]
    assert entry["environment"] == {"DUCKY_CONV_ID": "c1", "DUCKY_REQUESTED_MODE": "plan"}
    assert cfg.mcp_entry(str(tmp_path / "missing.json")) is None


def test_rules_isolate_chats_and_follow_modes(oc):
    cfg = oc("opencode_config")
    rules = cfg.session_rules("70182aa6-f997-4e28", mode="agent", approval="ask")
    assert rules[:2] == [{"action": "ducky-*", "resource": "*", "effect": "deny"},
                         {"action": "ducky-70182aa6f997*", "resource": "*", "effect": "allow"}]
    assert {"action": "edit", "resource": "*", "effect": "ask"} in rules
    assert {"action": "shell", "resource": "*", "effect": "ask"} in rules
    edits = cfg.session_rules("c", mode="agent", approval="edits")
    assert not any(r["action"] == "edit" for r in edits) and any(r["action"] == "shell" for r in edits)
    assert len(cfg.session_rules("c", mode="agent", approval="all")) == 2
    plan = cfg.session_rules("c", mode="plan", approval="all")
    assert {"action": "edit", "resource": "*", "effect": "deny"} in plan
    assert cfg.agent_for_mode("plan") == cfg.agent_for_mode("ask") == "plan" and cfg.agent_for_mode("agent") == "build"


def test_model_ref_never_sends_the_default_sentinel(oc):
    ref = oc("opencode_config").model_ref
    assert ref("default") is None and ref("") is None and ref("auto") is None
    assert ref("anthropic/claude-sonnet-5-5") == {"providerID": "anthropic", "id": "claude-sonnet-5-5"}
    assert ref("opencode/flint-40b-free", "high")["variant"] == "high"
    assert "variant" not in ref("opencode/flint-40b-free", "off")


def test_permission_requests_map_to_ducky_cards(oc, tmp_path):
    card = oc("opencode_config").permission_card
    tool, args = card({"action": "edit", "resources": ["a.txt"]}, str(tmp_path))
    assert tool == "Edit" and args["file_path"] == str(tmp_path / "a.txt")
    tool, args = card({"action": "shell", "resources": ["git status"], "metadata": {}}, str(tmp_path))
    assert (tool, args["command"]) == ("Bash", "git status")
    assert card({"action": "webfetch", "resources": ["https://x"]}, "")[0] == "webfetch"


def test_chat_tag_is_mcp_safe(oc):
    tag = oc("opencode_config").chat_tag
    assert tag("70182aa6-f997-4e28-b1a5") == "ducky-70182aa6f997"
    assert tag("") == "ducky-chat"
