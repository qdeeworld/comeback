"""Refusal coverage for direct invocation of an already configured script."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from comeback.hook import _handle_event, capability_invocation
from comeback.policy import detects_configured_argv, invocation_matches, invokes_configured_argv
from test_execution import _supervised_memory
from test_hook_fail_closed import _event


@pytest.mark.parametrize("agent_family", ["Codex", "ClaudeCode"])
@pytest.mark.parametrize("command", ["X --yes", "./X --yes", "{absolute} --yes", "env -u FOO ./X", "bash -c './X --yes'"])
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
])
def test_direct_script_refusal_does_not_guess_other_programs(tmp_path, command, argv):
    assert not detects_configured_argv(command, argv, working_directory=tmp_path)


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="POSIX shebang side-effect reproduction")
def test_real_direct_script_stops_before_side_effect(tmp_path):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", "X"])
    try:
        executable = tmp_path / "X"
        executable.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\nPath('released.txt').write_text('local fixture')\n", encoding="utf-8")
        executable.chmod(0o700)
        event = _event(memory, "./X --yes")
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
