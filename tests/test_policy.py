import shlex

import pytest

from comeback.policy import (
    checkpoint_invocation,
    classify_task,
    invocation_matches,
    is_success_wrapped,
    is_release_action,
    mode_for_outcomes,
    tool_succeeded,
)


def test_task_classification_keeps_unrelated_work_autonomous():
    assert classify_task("Update the README wording") == ("low_risk", "general")
    assert classify_task("Deploy the release to production") == (
        "release",
        "release_workflow",
    )


def test_supervision_evolves_with_outcomes():
    assert mode_for_outcomes(1, 0) == "HUMAN_REQUIRED"
    assert mode_for_outcomes(1, 1) == "CHECKPOINTED"
    assert mode_for_outcomes(1, 3) == "AUTONOMOUS"


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "command git push origin main",
        "/usr/bin/git push origin main",
        "git -C /tmp/repo push origin main",
        "env RELEASE=1 git push origin main",
        "bash -c 'git push origin main'",
        "eval 'git push origin main'",
        "echo ready;git push origin main",
        "pnpm run deploy",
        "npm publish",
        "./node_modules/.bin/wrangler deploy",
        "forge script Deploy.s.sol --broadcast",
        "python scripts/release_candidate.py",
        "(python scripts/release_candidate.py) && printf '\\nCOMEBACK_RELEASE_OK_123\\n'",
    ],
)
def test_recognized_release_command_spellings_are_protected(command):
    assert is_release_action(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )


@pytest.mark.parametrize(
    "command",
    [
        'echo "git push origin main"',
        "cat release_candidate.py",
        "type release_candidate.py",
        "sed -n '1,20p' release_candidate.py",
    ],
)
def test_release_words_in_read_only_arguments_are_not_treated_as_actions(command):
    assert not is_release_action(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )


@pytest.mark.parametrize(
    "response",
    [
        {"exit_code": 0, "output": "RELEASE CHECK PASSED"},
        {"result": {"exitCode": 0, "stdout": "RELEASE CHECK PASSED"}},
        {"output": "RELEASE CHECK PASSED\nexit code 0"},
        "RELEASE CHECK PASSED\nprocess exited with code 0",
    ],
)
def test_codex_success_response_shapes_are_recognized(response):
    assert tool_succeeded({"tool_response": response})


@pytest.mark.parametrize(
    "response",
    [
        {"exit_code": 1, "output": "RELEASE CHECK PASSED"},
        {"result": {"exitCode": 2}},
        {"output": "exit code 1"},
        None,
    ],
)
def test_failed_or_unknown_response_shapes_are_not_success(response):
    assert not tool_succeeded({"tool_response": response})


def test_checkpoint_invocation_prints_signed_marker_only_after_success():
    assert checkpoint_invocation("pnpm test", "COMEBACK_CHECK_OK_123") == (
        "(pnpm test) && printf '\\nCOMEBACK_CHECK_OK_123\\n'"
    )
    assert is_success_wrapped(
        "(pnpm test) && printf '\\nCOMEBACK_CHECK_OK_123\\n'",
        "COMEBACK_CHECK_OK_123",
    )
    assert not is_success_wrapped("pnpm test", "COMEBACK_CHECK_OK_123")


def test_same_repository_cd_prefix_preserves_exact_checkpoint_and_release_receipt(tmp_path):
    invocation = checkpoint_invocation("pnpm test", "COMEBACK_CHECK_OK_123")
    prefixed = f"cd {shlex.quote(str(tmp_path))} && {invocation}"

    assert invocation_matches(prefixed, invocation, working_directory=tmp_path)
    assert is_success_wrapped(
        prefixed,
        "COMEBACK_CHECK_OK_123",
        working_directory=tmp_path,
    )


def test_cd_prefix_cannot_substitute_another_repository(tmp_path):
    invocation = checkpoint_invocation("pnpm test", "COMEBACK_CHECK_OK_123")
    other = tmp_path / "other"
    prefixed = f"cd {shlex.quote(str(other))} && {invocation}"

    assert not invocation_matches(prefixed, invocation, working_directory=tmp_path)
    assert not is_success_wrapped(
        prefixed,
        "COMEBACK_CHECK_OK_123",
        working_directory=tmp_path,
    )


def test_direct_string_hook_response_requires_the_expected_marker():
    event = {"tool_response": "RELEASE CHECK PASSED\nCOMEBACK_CHECK_OK_123\n"}
    assert tool_succeeded(event, expected_marker="COMEBACK_CHECK_OK_123")
    assert not tool_succeeded(event, expected_marker="COMEBACK_CHECK_OK_OTHER")
