"""Offline regressions for the audited raw-command and hook error boundaries."""

import io
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.execution import execute_checkpoint, execute_release, reconcile_release
from comeback.hook import _handle_event, capability_invocation, main
from comeback.memory import InterventionMemory
from comeback.policy import (
    CommandParseError,
    comeback_capability_action,
    detects_configured_argv,
    invocation_matches,
    invokes_configured_argv,
    is_release_action,
)
from comeback.signing import reconciliation_fields, reconciliation_message
from test_execution import _approve, _supervised_memory


def _event(memory, command, *, session_id="fresh"):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_use_id": "audit-tool",
        "tool_input": {"command": command},
        "_comeback_agent_family": "Codex",
        "_comeback_memory_db": str(memory.db_path),
    }


@pytest.mark.parametrize("command", [
    "git push origin main #'",
    "git push origin main # it's fine",
    "comeback release --session-id fresh #'",
    "python deploy_prod.py #'",
    "bash -c \"git push origin main #'\"",
    "eval \"git push origin main #'\"",
])
def test_lexical_uncertainty_is_a_real_hook_denial(tmp_path, command):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", "deploy_prod.py"])
    try:
        output = _handle_event(_event(memory, command), root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "fail-closed" in output["hookSpecificOutput"]["permissionDecisionReason"]
        assert not (tmp_path / "released.txt").exists()
    finally:
        memory.close()


@pytest.mark.parametrize("command", [
    "echo harmless#literal; git push origin main",
    "echo 'harmless#literal'; git push origin main",
    r"echo harmless\#literal; git push origin main",
    "git push origin main # a balanced comment",
])
def test_literal_hash_does_not_erase_a_later_release(command):
    assert is_release_action({"tool_name": "Bash", "tool_input": {"command": command}})


def test_detector_cannot_turn_unparseable_input_into_false():
    event = {"tool_name": "Bash", "tool_input": {"command": "git push #'"}}
    with pytest.raises(CommandParseError):
        is_release_action(event)
    with pytest.raises(CommandParseError):
        comeback_capability_action(event)
    with pytest.raises(CommandParseError):
        detects_configured_argv(event["tool_input"]["command"], ["git", "push"])


@pytest.mark.parametrize("prefix", [
    "sudo -u deploy ", "sudo --user=deploy ", "sudo -udeploy ",
    "env -u FOO ", "env --unset=FOO ", "env -C /tmp ",
    "exec -a innocent ", "command -p ", "nohup ", "timeout 60 ",
    "timeout --signal TERM --kill-after=1s 60 ",
    ">out.log ", "> out.log ", "2>/dev/null ", "2> /dev/null ",
    "<>out.log ", "1<> out.log ", "<<<input ",
])
def test_explicit_wrappers_and_redirects_preserve_detection(prefix):
    event = {"tool_name": "Bash", "tool_input": {"command": prefix + "git push origin main"}}
    assert is_release_action(event)
    event["tool_input"]["command"] = prefix + "comeback release --session-id fresh"
    assert comeback_capability_action(event) == "release"
    assert detects_configured_argv(prefix + "python deploy_prod.py", ["python", "deploy_prod.py"])


@pytest.mark.parametrize("command", [
    "sudo --unsupported-option value git push origin main",
    "env -u", "timeout unknown git push origin main",
])
def test_unknown_wrapper_grammar_is_uncertainty_not_a_safe_miss(command):
    with pytest.raises(CommandParseError):
        is_release_action({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize("command", [
    "python deploy_prod.py", "python3 deploy_prod.py", "python3.13 deploy_prod.py",
    "python ./deploy_prod.py", "python deploy_prod.py --yes",
    "/usr/bin/python deploy_prod.py", "python deploy_prod.py # ordinary comment",
    "bash -c 'python3 ./deploy_prod.py --yes'", "env -u FOO python3 deploy_prod.py",
    'cd "$PWD" && python deploy_prod.py',
    "python -u deploy_prod.py", "python -I -B ./deploy_prod.py --yes",
    "python -IBu ./deploy_prod.py", "python -W ignore deploy_prod.py",
    "python -X utf8 deploy_prod.py", "python -Wignore -Xutf8 deploy_prod.py",
    "python --check-hash-based-pycs always deploy_prod.py",
    "python -- deploy_prod.py", "py -3 deploy_prod.py", "py -V:3.13 deploy_prod.py",
])
def test_configured_script_variants_are_denied_not_authorized(tmp_path, command):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", "deploy_prod.py"])
    try:
        event = _event(memory, command)
        assert detects_configured_argv(command, ["python", "deploy_prod.py"], working_directory=tmp_path)
        assert not invocation_matches(command, capability_invocation(event, "release", "fresh"), working_directory=tmp_path)
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert len(memory.pretool_decisions("fresh")) == 1
    finally:
        memory.close()


@pytest.mark.parametrize("command", [
    "cat deploy_prod.py", "echo python deploy_prod.py", "python other.py",
    "python3 -c 'print(1)'", "command -v python",
])
def test_configured_detection_keeps_unrelated_direct_commands_unmatched(tmp_path, command):
    assert not detects_configured_argv(command, ["python", "deploy_prod.py"], working_directory=tmp_path)


@pytest.mark.parametrize("command", [
    "python3 -I -m deploy_prod", "python -Imdeploy_prod --yes",
    "python -W ignore -m deploy_prod", "py -3.13 -m deploy_prod",
])
def test_configured_module_options_do_not_hide_the_entrypoint(tmp_path, command):
    memory, _ = _supervised_memory(tmp_path, release_argv=["python", "-m", "deploy_prod"])
    try:
        output = _handle_event(_event(memory, command), root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert not invokes_configured_argv(command, ["python", "-m", "deploy_prod"])
    finally:
        memory.close()


@pytest.mark.parametrize("command", [
    "python -u other.py", "python -W ignore other.py", "python -m other",
    "python -Ic 'print(1)' deploy_prod.py", "python -h deploy_prod.py",
    "python - deploy_prod.py",
])
def test_interpreter_options_do_not_turn_data_into_a_script(tmp_path, command):
    assert not detects_configured_argv(command, ["python", "deploy_prod.py"], working_directory=tmp_path)


@pytest.mark.parametrize("command", [
    "node --no-warnings ./deploy_prod.js", "node --require preload.js deploy_prod.js",
    "nodejs --env-file=.env deploy_prod.js --yes",
])
def test_node_options_preserve_configured_script_detection(tmp_path, command):
    assert detects_configured_argv(command, ["node", "deploy_prod.js"], working_directory=tmp_path)
    assert not invokes_configured_argv(command, ["node", "deploy_prod.js"])


@pytest.mark.parametrize("command", ["python --unknown value deploy_prod.py", "python -W", "node --unknown value deploy_prod.js"])
def test_unknown_interpreter_options_cannot_silently_miss_the_configured_action(command):
    argv = ["node", "deploy_prod.js"] if command.startswith("node") else ["python", "deploy_prod.py"]
    with pytest.raises(CommandParseError):
        detects_configured_argv(command, argv)


@pytest.mark.parametrize("command", [
    "python -Imcomeback.cli release --session-id fresh",
    "python -W ignore -m comeback.cli --db /tmp/other.db release --session-id fresh",
    "py -3.13 -m comeback.cli release --session-id fresh",
])
def test_interpreter_option_spellings_do_not_hide_noncanonical_capabilities(tmp_path, command):
    memory, _ = _supervised_memory(tmp_path)
    try:
        event = _event(memory, command)
        assert comeback_capability_action(event) == "release"
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        memory.close()


def test_python_script_arguments_are_not_reinterpreted_as_module_options():
    event = {"tool_name": "Bash", "tool_input": {"command": "python inspect_args.py -m comeback.cli release"}}
    assert comeback_capability_action(event) is None
    assert not is_release_action(event)


@pytest.mark.parametrize("command", [
    "python -W ignore scripts/release_candidate.py",
    "python -Iu scripts/release_candidate.py",
    "py -3.13 scripts/release_candidate.py",
])
def test_interpreter_options_preserve_builtin_candidate_classification(command):
    assert is_release_action({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize("command", [
    "python3 deploy_prod.py", "python deploy_prod.py --yes",
    "python deploy_prod.py && echo extra", "env -u FOO python deploy_prod.py",
    "/other/python deploy_prod.py", "python ./deploy_prod.py",
])
def test_strict_argv_matcher_does_not_inherit_risk_detector_widening(command):
    assert not invokes_configured_argv(command, ["python", "deploy_prod.py"])


@pytest.mark.parametrize("tool_input", [{}, {"command": ""}, {"command": 42}, None])
def test_missing_bash_command_is_not_an_unprotected_action(tmp_path, tool_input):
    memory, _ = _supervised_memory(tmp_path)
    try:
        event = _event(memory, "")
        event["tool_input"] = tool_input
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        memory.close()


@pytest.mark.parametrize("event_name", ["PreToolUse", "Stop", "UserPromptSubmit"])
@pytest.mark.parametrize("arguments", [["--bogus", "X"], ["--agent-family"], ["--cli-executable", ""]])
def test_launcher_argument_errors_keep_event_specific_blocking(event_name, arguments, monkeypatch, capsys):
    event = {"hook_event_name": event_name, "session_id": "argument-error"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "argv", ["comeback-hook", *arguments])
    main()
    output = json.loads(capsys.readouterr().out)
    if event_name == "PreToolUse":
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert output["decision"] == "block"


@pytest.mark.parametrize("payload", ["", "{invalid", "[]", "null", "{}", '{"hook_event_name":"unknown"}'])
def test_unreadable_hook_input_uses_blocking_exit_not_success(payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "argv", ["comeback-hook"])
    with pytest.raises(SystemExit) as raised:
        main()
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert "fail-closed" in captured.err


@pytest.mark.parametrize("event_name", ["UserPromptSubmit", "PreToolUse", "Stop"])
@pytest.mark.parametrize("session_id", [None, "", " ", 42])
def test_invalid_session_identity_has_explicit_blocking_response(tmp_path, event_name, session_id, monkeypatch, capsys):
    event = {"hook_event_name": event_name, "session_id": session_id, "cwd": str(tmp_path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "argv", ["comeback-hook", "--agent-family", "Codex"])
    main()
    output = json.loads(capsys.readouterr().out)
    if event_name == "PreToolUse":
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert output["decision"] == "block"
    assert "no session identity" in json.dumps(output)


def test_malformed_argv_in_actual_hook_process_emits_deny(tmp_path):
    source = str(Path(__file__).resolve().parents[1] / "src")
    launcher = f"import sys; sys.path.insert(0, {source!r}); from comeback.hook import main; main()"
    result = subprocess.run(
        [sys.executable, "-c", launcher, "--bogus", "X"],
        input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "argv-error"}),
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("exact", [False, True])
def test_real_satisfied_requirements_allow_only_exact_release_capability(tmp_path, exact):
    memory, owner = _supervised_memory(tmp_path)
    try:
        checkpoint, code = execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        assert code == 0 and checkpoint["decision"] == "checkpoint_recorded"
        _approve(memory, owner)
        run = memory.get_verified_run("fresh")
        assert memory.missing_requirements(run) == []
        event = _event(memory, "")
        event["tool_input"]["command"] = (
            capability_invocation(event, "release", "fresh")
            if exact else shlex.join(run["release_spec"]["argv"])
        )
        output = _handle_event(event, root=tmp_path, memory=memory)["hookSpecificOutput"]
        if exact:
            assert "signed release capability requirements satisfied" in output["additionalContext"]
            assert memory.pretool_decisions("fresh")[-1]["acted"]["decision"] == "allow"
        else:
            assert output["permissionDecision"] == "deny"
            assert "signed, one-shot release capability" in output["permissionDecisionReason"]
        assert not (tmp_path / "released.txt").exists()  # This tests permission, not execution.
    finally:
        memory.close()


@pytest.mark.parametrize("field,value", [("agent_family", "ClaudeCode"), ("repo_id", "another-repo"), ("session_id", "other-session")])
@pytest.mark.parametrize("event_name", ["PreToolUse", "Stop"])
def test_stored_run_cannot_select_another_principal(tmp_path, field, value, event_name):
    memory, _ = _supervised_memory(tmp_path)
    try:
        run = memory.get_run("fresh")
        run[field] = value
        memory.client.set_entity(memory.RUN_CATEGORY, "fresh", run, status="open")
        event = _event(memory, "git push origin main")
        event["hook_event_name"] = event_name
        output = _handle_event(event, root=tmp_path, memory=memory)
        if event_name == "PreToolUse":
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            assert output["decision"] == "block"
    finally:
        memory.close()


def test_unconfigured_release_does_not_claim_a_gate_was_satisfied(tmp_path):
    with InterventionMemory(tmp_path / "memory.db", "no-lessons") as memory:
        memory.start_run(session_id="fresh", task_class="release", area="release_workflow", agent_family="Codex", model="test")
        output = _handle_event(_event(memory, "git push origin main"), root=tmp_path, memory=memory)
        assert "no remembered intervention applies" in output["hookSpecificOutput"]["additionalContext"]
        assert "gate satisfied" not in json.dumps(output)


@pytest.mark.parametrize("stop_hook_active", [False, True])
def test_deleted_run_cannot_silently_disable_stop_supervision(tmp_path, stop_hook_active):
    memory, _ = _supervised_memory(tmp_path)
    try:
        memory.client.delete_entity(memory.RUN_CATEGORY, "fresh")
        event = _event(memory, "")
        event.update(hook_event_name="Stop", stop_hook_active=stop_hook_active)
        output = _handle_event(event, root=tmp_path, memory=memory)
        if stop_hook_active:
            assert output["continue"] is False
            reason = output["systemMessage"]
        else:
            assert output["decision"] == "block"
            reason = output["reason"]
        assert "no Sibyl supervision run exists" in reason
        assert "fresh session" in reason
    finally:
        memory.close()


@pytest.mark.parametrize("status,outcome", [("completed", "success"), ("failed", "failure")])
def test_unsigned_closed_status_cannot_end_stop_supervision(tmp_path, status, outcome):
    memory, _ = _supervised_memory(tmp_path)
    try:
        run = memory.get_run("fresh")
        run.update(status=status, outcome=outcome)
        memory.client.set_entity(memory.RUN_CATEGORY, "fresh", run, status=status)
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["decision"] == "block"
        assert "no matching lesson outcome" in output["reason"]
    finally:
        memory.close()


def test_closed_run_bound_to_a_missing_lesson_blocks_instead_of_crashing(tmp_path):
    memory, _ = _supervised_memory(tmp_path)
    try:
        run = memory.get_run("fresh")
        run.update(status="completed", outcome="success", lesson_ids=["missing"], lesson_revisions={"missing": 1})
        memory.client.set_entity(memory.RUN_CATEGORY, "fresh", run, status="completed")
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["decision"] == "block"
        assert "missing lesson" in output["reason"]
    finally:
        memory.close()


def test_genuinely_completed_release_still_stops_normally(tmp_path):
    memory, owner = _supervised_memory(tmp_path)
    try:
        execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        _approve(memory, owner)
        execute_release(memory, session_id="fresh", root=tmp_path)
        assert memory.get_run("fresh")["status"] == "completed"
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        assert _handle_event(event, root=tmp_path, memory=memory) is None
    finally:
        memory.close()


def test_owner_reconciled_release_still_stops_normally(tmp_path):
    memory, owner = _supervised_memory(tmp_path, release_argv=[sys.executable, "-c", "raise SystemExit(1)"])
    try:
        execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        _approve(memory, owner)
        execute_release(memory, session_id="fresh", root=tmp_path)
        run = memory.get_run("fresh")
        assert run["status"] == "unknown"
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        assert "operator reconciliation is required" in _handle_event(event, root=tmp_path, memory=memory)["reason"]
        resolved_at = datetime.now(timezone.utc).isoformat()
        signature = Account.sign_message(
            encode_defunct(text=reconciliation_message(run, "not_released", resolved_at)), private_key=owner.key
        ).signature.hex()
        reconcile_release(memory, session_id="fresh", root=tmp_path, resolution="not_released", resolved_at=resolved_at, signature=signature)
        assert memory.get_run("fresh")["status"] == "failed"
        assert _handle_event(event, root=tmp_path, memory=memory) is None
    finally:
        memory.close()


@pytest.mark.parametrize("closer_signs", [False, True])
def test_closed_run_reconciliation_must_be_signed_by_the_authorized_closer(tmp_path, closer_signs):
    memory, owner = _supervised_memory(tmp_path)
    try:
        run = memory.get_run("fresh")
        signer = owner if closer_signs else Account.create()
        resolved_at = datetime.now(timezone.utc).isoformat()
        signature = Account.sign_message(
            encode_defunct(text=reconciliation_message(run, "not_released", resolved_at)), private_key=signer.key
        ).signature.hex()
        reconciliation = {
            "resolution": "not_released", "resolved_at": resolved_at, "signer": signer.address.lower(),
            "signature": signature, "signed_fields": reconciliation_fields(run, "not_released", resolved_at),
        }
        run.update(status="failed", outcome="failure", outcome_reason="reserved_release_never_started", reconciliation=reconciliation)
        memory.client.set_entity(memory.RUN_CATEGORY, "fresh", run, status="failed")
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        output = _handle_event(event, root=tmp_path, memory=memory)
        if closer_signs:
            assert output is None
        else:
            assert output["decision"] == "block"
            assert "not signed by the authorized closer" in output["reason"]
    finally:
        memory.close()


def test_stop_without_lifecycle_activation_is_not_success(tmp_path):
    with InterventionMemory(tmp_path / "memory.db", "no-lifecycle") as memory:
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        output = _handle_event(event, root=tmp_path, memory=memory)
        assert output["decision"] == "block"
        assert "doctor" in output["reason"]


def test_initialized_low_risk_run_can_stop_normally(tmp_path):
    with InterventionMemory(tmp_path / "memory.db", "low-risk") as memory:
        memory.start_run(session_id="fresh", task_class="low_risk", area="general", agent_family="Codex", model="test")
        event = _event(memory, "")
        event["hook_event_name"] = "Stop"
        assert _handle_event(event, root=tmp_path, memory=memory) is None


def test_unsigned_requirements_cannot_override_matching_signed_lesson(tmp_path):
    memory, _ = _supervised_memory(tmp_path)
    try:
        run = memory.get_run("fresh")
        run.update(mode="AUTONOMOUS", lesson_ids=[], lesson_revisions={}, required_evidence=[], action_schema=None, checkpoint_spec=None, release_spec=None, state_policy=None)
        memory.client.set_entity(memory.RUN_CATEGORY, "fresh", run, status="open")
        output = _handle_event(_event(memory, "git push origin main"), root=tmp_path, memory=memory)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "binding" in output["hookSpecificOutput"]["permissionDecisionReason"]
    finally:
        memory.close()


def test_unsupported_launcher_agent_cannot_evade_same_agent_lessons(tmp_path, monkeypatch, capsys):
    event = {"hook_event_name": "PreToolUse", "session_id": "bad-agent", "cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "argv", ["comeback-hook", "--agent-family", "OtherAgent"])
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "unsupported" in output["hookSpecificOutput"]["permissionDecisionReason"]
