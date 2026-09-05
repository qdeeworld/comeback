"""Offline fixtures for Claude stream verification; not authenticated evidence."""
import copy
import hashlib
import json
import subprocess

import pytest

from scripts.run_claude_unlock_gate import _execution_evidence, _run_claude


def fixture(retry=False):
    commands = ["checkpoint", "release"]
    if retry:
        commands.insert(0, "checkpoint")
    messages, decisions = [], []
    for i, command in enumerate(commands):
        identity = f"tool-{i}"
        messages.append({"type": "assistant", "session_id": "fresh", "message": {
            "content": [{"type": "tool_use", "id": identity, "name": "Bash",
                         "input": {"command": command}}]}})
        failed = retry and i == 0
        result = "Exit code 126\nPermission denied" if failed else json.dumps({
            "session_id": "fresh", "exit_code": 0,
            "decision": "checkpoint_recorded" if command == "checkpoint" else "release_completed",
        })
        messages.append({"type": "user", "session_id": "fresh", "message": {
            "content": [{"type": "tool_result", "tool_use_id": identity,
                         "is_error": failed, "content": result}]}})
        decisions.append({"evaluated": {"session_id": "fresh", "tool_use_id": identity,
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "action_kind": command + "_capability"}, "acted": {"decision": "allow"}})
    messages.append({"type": "result", "session_id": "fresh", "is_error": False,
                     "subtype": "success", "permission_denials": []})
    events = [{"evaluated": {"session_id": "fresh"}, "acted": {"event": kind}}
              for kind in ["checkpoint_started", "checkpoint_passed"]]
    return messages, decisions, events


def verify(messages, decisions, events, witness=True):
    return _execution_evidence("\n".join(map(json.dumps, messages)), decisions,
        session_id="fresh", checkpoint="checkpoint", release="release",
        checkpoint_events=events, checkpoint_witness=witness)


@pytest.mark.parametrize("retry", [False, True])
def test_execution_evidence_accepts_proven_lifecycle(retry):
    result = verify(*fixture(retry))
    assert result["valid"] is True
    assert result["retry_count"] == int(retry)


@pytest.mark.parametrize("mutation", [
    "missing_result", "duplicate_result", "duplicate_call", "wrong_session",
    "wrong_command", "wrong_hash", "wrong_tool_id", "denial", "missing_permission",
    "parallel", "background", "wrong_order", "missing_terminal", "duplicate_terminal",
    "terminal_error", "missing_witness", "double_execution", "missing_started",
    "failed_execution", "narrative_only", "retry_success", "wrong_exit", "third_retry",
    "missing_denials_field", "result_after_terminal", "false_success_json",
])
def test_execution_evidence_rejects_unproven_or_unsafe_runs(mutation):
    messages, decisions, events = fixture(True)
    witness = True
    if mutation == "missing_result":
        del messages[1]
    elif mutation == "duplicate_result":
        messages.insert(2, copy.deepcopy(messages[1]))
    elif mutation == "duplicate_call":
        messages[2]["message"]["content"][0]["id"] = "tool-0"
    elif mutation == "wrong_session":
        messages[0]["session_id"] = "other"
    elif mutation == "wrong_command":
        messages[2]["message"]["content"][0]["input"]["command"] = "checkpoint; bypass"
    elif mutation == "wrong_hash":
        decisions[0]["evaluated"]["command_sha256"] = "wrong"
    elif mutation == "wrong_tool_id":
        decisions[0]["evaluated"]["tool_use_id"] = "other"
    elif mutation == "denial":
        decisions[0]["acted"]["decision"] = "deny"
    elif mutation == "missing_permission":
        decisions.pop()
    elif mutation == "parallel":
        messages[1], messages[2] = messages[2], messages[1]
    elif mutation == "background":
        messages[0]["message"]["content"][0]["input"]["run_in_background"] = True
    elif mutation == "wrong_order":
        messages[:2], messages[4:6] = messages[4:6], messages[:2]
    elif mutation == "missing_terminal":
        messages.pop()
    elif mutation == "duplicate_terminal":
        messages.append(copy.deepcopy(messages[-1]))
    elif mutation == "terminal_error":
        messages[-1]["is_error"] = True
    elif mutation == "missing_witness":
        witness = False
    elif mutation == "double_execution":
        events.append(copy.deepcopy(events[0]))
    elif mutation == "missing_started":
        events.pop(0)
    elif mutation == "failed_execution":
        events.append({"evaluated": {"session_id": "fresh"},
                       "acted": {"event": "checkpoint_not_passed"}})
    elif mutation == "narrative_only":
        messages[1]["message"]["content"] = [{"type": "text", "text": "Exit code 126 Permission denied"}]
    elif mutation == "retry_success":
        messages[1]["message"]["content"][0]["is_error"] = False
    elif mutation == "wrong_exit":
        messages[1]["message"]["content"][0]["content"] = "Exit code 1 Permission denied"
    elif mutation == "third_retry":
        messages[2:2] = copy.deepcopy(messages[:2])
    elif mutation == "missing_denials_field":
        del messages[-1]["permission_denials"]
    elif mutation == "result_after_terminal":
        messages.append({"type": "system"})
    elif mutation == "false_success_json":
        messages[3]["message"]["content"][0]["content"] = '{"decision":"checkpoint_recorded","exit_code":true,"session_id":"fresh"}'
    with pytest.raises(ValueError):
        verify(messages, decisions, events, witness)


def test_unlock_stream_flags_and_stdin(monkeypatch, tmp_path):
    captured = {}
    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
    monkeypatch.setattr("scripts.run_claude_unlock_gate.subprocess.run", run)
    _run_claude(tmp_path / "claude", root=tmp_path, environment={}, session_id="fresh",
                prompt="release this", tools="Bash", execution_trace=True)
    assert captured["command"][captured["command"].index("--output-format") + 1] == "stream-json"
    assert "--verbose" in captured["command"]
    assert captured["input"] == "release this"


def test_nested_gate_refuses_before_launch(monkeypatch):
    from scripts.run_claude_unlock_gate import run_gate
    monkeypatch.setenv("CLAUDECODE", "1")
    def forbidden(*args, **kwargs):
        pytest.fail("Nested run must refuse before launching Claude")
    monkeypatch.setattr("scripts.run_claude_unlock_gate.subprocess.run", forbidden)
    result, code = run_gate()
    assert code == 1 and "normal terminal" in result["error"]


def test_timeout_preserves_partial_stream(monkeypatch, tmp_path):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 300, output=b'{"type":"system"}\n', stderr=b'partial error')
    monkeypatch.setattr("scripts.run_claude_unlock_gate.subprocess.run", timeout)
    result = _run_claude(tmp_path / "claude", root=tmp_path, environment={},
                        session_id="fresh", prompt="release this", execution_trace=True)
    assert result.returncode == 124
    assert result.stdout == '{"type":"system"}\n'
    assert "partial error" in result.stderr
