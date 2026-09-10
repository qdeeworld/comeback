import copy
import hashlib

import pytest

from scripts.run_cross_agent_gate import _has_matching_sibyl_denial
from scripts.run_validation_gate import decision


@pytest.mark.parametrize("output", [
    None, [], "allow",
    {}, {"systemMessage": "hook failed"}, {"hookSpecificOutput": {}},
    {"hookSpecificOutput": {"hookEventName": "Stop", "permissionDecision": "allow"}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse"}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ""}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": {}}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": []}},
])
def test_missing_or_invalid_hook_evidence_is_not_allow(output):
    with pytest.raises(ValueError):
        decision(output)


@pytest.mark.parametrize("specific,expected", [
    ({"permissionDecision": "deny"}, "deny"),
    ({"permissionDecision": "allow"}, "allow"),
    ({"additionalContext": "Comeback: exact checkpoint capability allowed."}, "allow"),
])
def test_explicit_pretool_evidence(specific, expected):
    assert decision({"hookSpecificOutput": {"hookEventName": "PreToolUse", **specific}}) == expected


def denial_fixture():
    command = "python release_candidate.py"
    denial = {"tool_name": "Bash", "tool_use_id": "tool-exact",
              "tool_input": {"command": command}}
    event = {"evaluated": {
        "session_id": "fresh", "tool_use_id": "tool-exact",
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "action_kind": "raw_release", "mode": "HUMAN_REQUIRED",
    }, "acted": {"event": "pretool_decision", "decision": "deny"}}
    return command, denial, event


def test_cross_agent_denial_requires_real_matching_sibyl_event():
    command, denial, event = denial_fixture()
    assert _has_matching_sibyl_denial([denial], [event], session_id="fresh", command=command)
    assert not _has_matching_sibyl_denial([denial], [], session_id="fresh", command=command)
    assert not _has_matching_sibyl_denial([], [event], session_id="fresh", command=command)
    assert not _has_matching_sibyl_denial([denial, denial], [event], session_id="fresh", command=command)
    assert not _has_matching_sibyl_denial([denial], [event, event], session_id="fresh", command=command)
    assert not _has_matching_sibyl_denial([None], [event], session_id="fresh", command=command)
    assert not _has_matching_sibyl_denial([denial], [None], session_id="fresh", command=command)


@pytest.mark.parametrize("section,field,value", [
    ("evaluated", "session_id", "another-session"),
    ("evaluated", "tool_use_id", "other-tool"),
    ("evaluated", "command_sha256", "0" * 64),
    ("evaluated", "mode", "AUTONOMOUS"),
    ("evaluated", "action_kind", "checkpoint_capability"),
    ("acted", "decision", "allow"),
    ("acted", "event", "something_else"),
])
def test_cross_agent_does_not_credit_different_or_allowed_event(section, field, value):
    command, denial, event = denial_fixture()
    event = copy.deepcopy(event)
    event[section][field] = value
    assert not _has_matching_sibyl_denial([denial], [event], session_id="fresh", command=command)


@pytest.mark.parametrize("field,value", [("tool_name", "Other"), ("tool_use_id", None),
                                       ("tool_input", {"command": "python another.py"})])
def test_cross_agent_does_not_credit_unrelated_harness_denial(field, value):
    command, denial, event = denial_fixture()
    denial[field] = value
    assert not _has_matching_sibyl_denial([denial], [event], session_id="fresh", command=command)
