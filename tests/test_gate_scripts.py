import json
import subprocess
from pathlib import Path

import pytest

from comeback.policy import classify_task
from scripts.run_claude_unlock_gate import _run_claude as _run_claude_unlock
from scripts.run_claude_unlock_gate import (
    _fresh_capability_prompt,
    _permission_denials,
    _valid_sibyl_capability_trace,
)
from scripts.run_cross_agent_gate import _run_claude as _run_claude_cross_agent


@pytest.mark.parametrize(
    "runner,tools",
    [
        (_run_claude_cross_agent, ""),
        (_run_claude_cross_agent, "Bash"),
        (_run_claude_unlock, ""),
        (_run_claude_unlock, "Bash"),
    ],
)
def test_claude_gate_sends_prompt_over_stdin_after_variadic_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner,
    tools: str,
) -> None:
    invocation: dict[str, object] = {}

    def fake_run(command, **kwargs):
        invocation["command"] = command
        invocation["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.__globals__["subprocess"], "run", fake_run)
    prompt = "PROMPT_MUST_NOT_BECOME_A_TOOL_NAME"

    runner(
        Path("claude"),
        root=tmp_path,
        environment={},
        session_id="00000000-0000-4000-8000-000000000000",
        prompt=prompt,
        tools=tools,
    )

    assert invocation["input"] == prompt
    assert prompt not in invocation["command"]
    tools_index = invocation["command"].index("--tools")
    assert invocation["command"][tools_index + 1] == tools


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
