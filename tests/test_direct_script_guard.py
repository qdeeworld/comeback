"""Refusal coverage for direct invocation of an already configured script."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from comeback.hook import _handle_event, capability_invocation
from comeback.policy import _shell_words, detects_configured_argv, invocation_matches, invokes_configured_argv
from test_execution import _supervised_memory
from test_hook_fail_closed import _event


@pytest.mark.parametrize("agent_family", ["Codex", "ClaudeCode"])
@pytest.mark.parametrize("command", ["./X --yes", "{absolute} --yes", "env -u FOO ./X", "bash -c './X --yes'"])
def test_same_configured_script_direct_execution_is_denied(tmp_path, command, agent_family):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", "X"])
    try:
        session = "direct-" + agent_family
        memory.start_run(session_id=session, task_class="release", area="release_workflow", agent_family=agent_family, model="test")
        command = command.format(absolute=shlex.quote(str(tmp_path / "X")))
        event = _event(memory, command, session_id=session)
        event["_comeback_agent_family"] = agent_family
        assert detects_configured_argv(command, ["python", "X"], working_directory=tmp_path)
        assert not invokes_configured_argv(command, ["python", "X"], working_directory=tmp_path)
        assert not invocation_matches(command, capability_invocation(event, "release", session), working_directory=tmp_path)
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        decisions = memory.pretool_decisions(session)
        assert len(decisions) == 1
        assert decisions[0]["evaluated"]["action_kind"] == "raw_release"
    finally:
        memory.close()


@pytest.mark.parametrize("command,argv", [
    ("./X --yes", ["python", "-I", "X"]),
    ("./X --yes", ["node", "--no-warnings", "X"]),
    ("./X --yes", ["python", "X", "--approved-argument"]),
])
def test_known_script_direct_execution_is_refusal_only(tmp_path, command, argv):
    assert detects_configured_argv(command, argv, working_directory=tmp_path)
    assert not invokes_configured_argv(command, argv, working_directory=tmp_path)


@pytest.mark.parametrize("command,argv", [
    ("cat X", ["python", "X"]),
    ("echo X", ["python", "X"]),
    ("./other/X", ["python", "X"]),
    ("./Y", ["python", "X"]),
    ("./X", ["python", "-m", "X"]),
    ("./X", ["python", "-c", "X"]),
    ("./X", ["python", "--unknown", "X"]),
    ("X --yes", ["python", "X"]),
])
def test_direct_script_refusal_does_not_guess_other_programs(tmp_path, command, argv):
    assert not detects_configured_argv(command, argv, working_directory=tmp_path)


@pytest.mark.parametrize("command", [
    "cd scripts && ./X", "cd scripts; ./X", "pushd scripts && ./X",
    "bash -c 'cd scripts && ./X'", "env -C scripts ./X",
    "env --chdir=scripts ./X", "sudo -D scripts ./X",
    "cd scripts && python X",
    "eval 'cd scripts'; ./X", "env -Cscripts ./X",
    "command cd scripts && ./X", "command -p cd scripts && ./X",
    "command -- cd scripts && ./X", "command command cd scripts && ./X",
    "builtin cd scripts && ./X", "command eval 'cd scripts'; ./X",
    "command eval 'cd scripts; ./X'",
])
def test_directory_changes_cannot_hide_a_known_script(tmp_path, command):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", "scripts/X"])
    try:
        assert detects_configured_argv(command, ["python", "scripts/X"], working_directory=tmp_path)
        output = _handle_event(_event(memory, command), root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        memory.close()


@pytest.mark.parametrize("command", [
    "cd scripts && cat X", "cd scripts && ./Y", "cd scripts && {other_absolute}",
    "command -v cd; ./X", "command -V cd; ./X",
])
def test_directory_uncertainty_does_not_match_unrelated_actions(tmp_path, command):
    command = command.format(other_absolute=shlex.quote(str(tmp_path / "unrelated" / "X")))
    assert not detects_configured_argv(command, ["python", "scripts/X"], working_directory=tmp_path)


def test_refusal_tokenizer_can_preserve_windows_backslashes():
    assert _shell_words(r".\X --yes", preserve_backslashes=True) == [r".\X", "--yes"]
    assert _shell_words(r"C:\repo\X --yes", preserve_backslashes=True) == [r"C:\repo\X", "--yes"]
    # Default parsing stays unchanged for actual POSIX escape handling.
    assert _shell_words(r".\X --yes") == [".X", "--yes"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows path resolution")
@pytest.mark.parametrize("command,script", [
    (r".\X --yes", "X"), (r"& .\X --yes", "X"),
    ("{absolute} --yes", "X"), (r"cd scripts && .\X", "scripts/X"),
    (r"Set-Location scripts; .\X", "scripts/X"),
    (r"pwsh -Command 'cd scripts; .\X'", "scripts/X"),
    (r"cd scripts && .\x", "scripts/X"),
    ("& {drive}X", "X"), ("{drive}X --yes", "X"),
    ("cd scripts; & {drive}X", "scripts/X"),
])
def test_native_windows_direct_paths_reach_hook_denial(tmp_path, command, script):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", script])
    try:
        command = command.format(absolute=str(tmp_path / "X"), drive=tmp_path.drive)
        assert detects_configured_argv(command, ["python", script], working_directory=tmp_path)
        output = _handle_event(_event(memory, command), root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        memory.close()


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="POSIX shebang side-effect reproduction")
@pytest.mark.parametrize("command,script", [
    ("./X --yes", "X"), ("cd scripts && ./X --yes", "scripts/X"),
    ("command cd scripts && ./X --yes", "scripts/X"),
])
def test_real_direct_script_stops_before_side_effect(tmp_path, command, script):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", script])
    try:
        executable = tmp_path / script
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(tmp_path / 'released.txt')!r}).write_text('local fixture')\n", encoding="utf-8")
        executable.chmod(0o700)
        event = _event(memory, command)
        output = _handle_event(event, root=tmp_path, memory=memory)
        denied = bool(output and output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
        if not denied:
            environment = os.environ.copy()
            environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
            subprocess.run(["sh", "-c", event["tool_input"]["command"]], cwd=tmp_path, env=environment, check=True, timeout=10)
        assert not (tmp_path / "released.txt").exists()
        assert denied
    finally:
        memory.close()
