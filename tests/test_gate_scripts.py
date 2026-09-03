import json

import pytest

from comeback.policy import classify_task
from scripts.run_claude_unlock_gate import (
    _fresh_capability_prompt,
    _permission_denials,
    _valid_sibyl_capability_trace,
)


def test_claude_unlock_prompt_is_release_classified_before_first_tool() -> None:
    prompt = _fresh_capability_prompt(
        "comeback checkpoint --session-id fresh",
        "comeback release --session-id fresh",
    )

    assert classify_task(prompt) == ("release", "release_workflow")


def test_claude_unlock_gate_keeps_prefixed_permission_denials() -> None:
    denial = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "cd 'C:/temporary/repo' && comeback release --session-id fresh"
        },
    }

    assert _permission_denials(json.dumps({"permission_denials": [denial]})) == [
        denial
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        "{}",
        '{"permission_denials": {}}',
        '{"permission_denials": ["not-an-object"]}',
    ],
)
def test_claude_unlock_gate_rejects_unverifiable_denial_output(payload: str) -> None:
    with pytest.raises(ValueError):
        _permission_denials(payload)


def test_claude_unlock_gate_rejects_duplicate_checkpoint_allows() -> None:
    duplicate_checkpoint = [
        {
            "evaluated": {"action_kind": "checkpoint_capability"},
            "acted": {"decision": "allow"},
        },
        {
            "evaluated": {"action_kind": "checkpoint_capability"},
            "acted": {"decision": "allow"},
        },
    ]

    assert _valid_sibyl_capability_trace(duplicate_checkpoint) is False


def test_claude_unlock_gate_accepts_one_checkpoint_and_one_release() -> None:
    complete_trace = [
        {
            "evaluated": {"action_kind": "checkpoint_capability"},
            "acted": {"decision": "allow"},
        },
        {
            "evaluated": {"action_kind": "release_capability"},
            "acted": {"decision": "allow"},
        },
    ]

    assert _valid_sibyl_capability_trace(complete_trace) is True
