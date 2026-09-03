import json

from comeback.policy import classify_task
from scripts.run_claude_unlock_gate import (
    _fresh_capability_prompt,
    _permission_denials,
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
