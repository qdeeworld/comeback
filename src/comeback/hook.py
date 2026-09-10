from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from .identity import repository_configuration
from .launcher import launcher_argv
from .memory import InterventionMemory, MemoryIntegrityError
from .policy import (
    CommandParseError,
    WORKFLOW_AREAS,
    classify_task,
    comeback_capability_action,
    command_from_event,
    detects_configured_argv,
    is_release_action,
    is_release_capability,
)


def _database(root: Path, event: dict[str, Any]) -> Path:
    selected = event.get("_comeback_memory_db")
    if isinstance(selected, str) and selected:
        path = Path(selected).expanduser()
        if not path.is_absolute():
            raise MemoryIntegrityError("trusted memory database path must be absolute")
        return path.resolve()
    configured = os.environ.get("COMEBACK_MEMORY_DB")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise MemoryIntegrityError("COMEBACK_MEMORY_DB must be an absolute path")
        return path.resolve()
    return root / ".comeback" / "memory.db"


def _agent_family(event: dict[str, Any]) -> str:
    override = event.get("_comeback_agent_family")
    family = (
        override if isinstance(override, str) and override
        else os.environ.get("COMEBACK_AGENT_FAMILY", "Codex")
    )
    if family not in {"Codex", "ClaudeCode"}:
        raise MemoryIntegrityError("installed hook agent family is unsupported")
    return family


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def capability_invocation(
    event: dict[str, Any], action: str, session_id: str
) -> str:
    configured = event.get("_comeback_cli_executable")
    if isinstance(configured, str) and configured:
        executable = Path(configured).expanduser()
        if not executable.is_absolute():
            raise MemoryIntegrityError("trusted capability executable must be absolute")
        argv = launcher_argv(executable, "comeback.cli")
    else:
        # Programmatic callers and the deterministic harness do not enter through
        # the installed console hook. Keep their launcher exact as well.
        argv = [sys.executable, "-m", "comeback.cli"]
    selected_database = event.get("_comeback_memory_db")
    if not isinstance(selected_database, str) or not selected_database:
        raise MemoryIntegrityError("hook has no selected Sibyl memory database")
    database = Path(selected_database).expanduser()
    if not database.is_absolute():
        raise MemoryIntegrityError("selected Sibyl memory database must be absolute")
    argv.extend(
        ["--db", str(database.resolve()), action, "--session-id", session_id]
    )
    if os.name == "nt" and _agent_family(event) == "Codex":
        return "& " + " ".join(_powershell_quote(argument) for argument in argv)
    return shlex.join(argv)


def _context(run: dict[str, Any], event: dict[str, Any]) -> str:
    if run["status"] != "open":
        return (
            f"Comeback supervision session is already {run['status']}. "
            "Start a genuinely fresh agent session before another release attempt."
        )
    lessons = len(run["lesson_ids"])
    requirements = ", ".join(run["required_evidence"]) or "none"
    checkpoint = (
        capability_invocation(event, "checkpoint", run["session_id"])
        if "release_check_passed" in run["required_evidence"]
        else "ordinary repository checks"
    )
    release = (
        capability_invocation(event, "release", run["session_id"])
        if run["lesson_ids"]
        else "the ordinary repository release command"
    )
    return (
        f"Comeback supervision: {run['mode']}. "
        f"Workflow: {run['area']}. "
        f"Recalled intervention lessons: {lessons}. Required evidence: {requirements}. "
        f"Checkpoint capability: {checkpoint}. Release capability: {release}. "
        "Do not claim completion while this run remains open."
    )


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _block_stop(reason: str, event: dict[str, Any]) -> dict[str, Any]:
    if event.get("stop_hook_active"):
        return {"continue": False, "systemMessage": reason}
    return {"decision": "block", "reason": reason}


def _pretool_result(
    memory: InterventionMemory,
    event: dict[str, Any],
    *,
    action_kind: str,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    memory.record_pretool_decision(
        session_id=str(event["session_id"]),
        tool_use_id=str(event.get("tool_use_id", "unknown")),
        command=command_from_event(event),
        action_kind=action_kind,
        decision=decision,
        reason=reason,
    )
    if decision == "deny":
        return _deny(reason)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": reason,
        }
    }


def _verify_run_principal(
    run: dict[str, Any], event: dict[str, Any], memory: InterventionMemory
) -> None:
    # A self-consistent stored run must not choose a different principal to
    # escape same-agent lessons. The reviewed launcher supplies this identity.
    if run.get("repo_id") != memory.repo_id:
        raise MemoryIntegrityError("supervision run belongs to another repository")
    if run.get("agent_family") != _agent_family(event):
        raise MemoryIntegrityError("supervision run agent differs from the installed hook")
    if run.get("session_id") != event.get("session_id"):
        raise MemoryIntegrityError("supervision run belongs to another session")


def _handle_event(
    event: dict[str, Any],
    *,
    root: Path,
    memory: InterventionMemory,
) -> dict[str, Any] | None:
    """Process one lifecycle event using an already-owned memory handle."""

    session_id = event.get("session_id")
    event_name = event.get("hook_event_name")

    if not isinstance(session_id, str) or not session_id.strip():
        raise MemoryIntegrityError("hook event has no session identity")

    if event_name == "PreToolUse" and event.get("tool_name") == "Bash" and not command_from_event(event).strip():
        return _deny("Comeback fail-closed: Bash hook input has no non-empty command.")

    if event_name == "UserPromptSubmit":
        prompt = str(event.get("prompt", ""))
        task_class, area = classify_task(prompt)
        run = memory.start_run(
            session_id=session_id,
            task_class=task_class,
            area=area,
            agent_family=_agent_family(event),
            model=str(event.get("model", "unknown")),
            process_id=os.getpid(),
        )
        _verify_run_principal(run, event, memory)
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _context(run, event),
            }
        }

    release_command = command_from_event(event).strip()
    expected_checkpoint = capability_invocation(event, "checkpoint", session_id)
    expected_release = capability_invocation(event, "release", session_id)
    exact_checkpoint = is_release_capability(
        release_command,
        expected_checkpoint,
        working_directory=root,
    )
    exact_release = is_release_capability(
        release_command,
        expected_release,
        working_directory=root,
    )
    configured_raw_action = False
    configured_areas: set[str] = set()
    preliminary_run: dict[str, Any] | None = None
    try:
        comeback_action = comeback_capability_action(event)
        release_action = is_release_action(event)
        if event_name == "PreToolUse":
            try:
                preliminary_run = memory.get_run(session_id)
            except MemoryIntegrityError:
                preliminary_run = None
            if preliminary_run is not None:
                _verify_run_principal(preliminary_run, event, memory)
            configured_areas = {
                area for area, release_argv in memory.configured_workflow_actions(_agent_family(event))
                if detects_configured_argv(
                    release_command,
                    release_argv,
                    working_directory=root,
                )
            }
            configured_raw_action = bool(configured_areas)
    except (CommandParseError, MemoryIntegrityError) as exc:
        if event_name == "PreToolUse":
            return _deny(f"Comeback fail-closed: {exc}. Use a direct, unambiguous command.")
        raise
    if event_name == "PreToolUse" and (
        release_action
        or exact_checkpoint
        or exact_release
        or comeback_action is not None
        or configured_raw_action
    ):
        action_kind = (
            "checkpoint_capability"
            if exact_checkpoint
            else (
                "release_capability"
                if exact_release
                else (
                    "noncanonical_capability"
                    if comeback_action is not None
                    else "raw_release"
                )
            )
        )
        try:
            run = preliminary_run or memory.get_run(session_id)
        except MemoryIntegrityError as exc:
            return _deny(f"Comeback fail-closed: {exc}")
        if run["status"] != "open":
            return _pretool_result(
                memory,
                event,
                action_kind=action_kind,
                decision="deny",
                reason=(
                    f"Comeback session is already {run['status']}; start a genuinely fresh "
                    "agent session before another release attempt."
                ),
            )
        try:
            run = memory.get_verified_run(session_id)
            _verify_run_principal(run, event, memory)
        except MemoryIntegrityError as exc:
            return _pretool_result(
                memory,
                event,
                action_kind=action_kind,
                decision="deny",
                reason=f"Comeback fail-closed: {exc}",
            )
        if len(configured_areas) > 1:
            return _pretool_result(memory, event, action_kind=action_kind, decision="deny",
                reason="Comeback refuses an action matching multiple signed workflows.")
        selected_area = (
            next(iter(configured_areas)) if configured_areas
            else run["area"] if exact_checkpoint or exact_release or comeback_action is not None
            else "release_workflow"
        )
        if run["task_class"] == "release" and run["area"] != selected_area:
            return _pretool_result(memory, event, action_kind=action_kind, decision="deny",
                reason="Comeback workflow mismatch: start a fresh session for the other workflow.")
        if run["task_class"] != "release":
            run = memory.start_run(
                session_id=session_id,
                task_class="release",
                area=selected_area if selected_area in WORKFLOW_AREAS else "release_workflow",
                agent_family=_agent_family(event),
                model=str(event.get("model", "unknown")),
                process_id=os.getpid(),
                force=True,
            )
        if comeback_action == "checkpoint" or exact_checkpoint:
            if (
                not run["lesson_ids"]
                or "release_check_passed" not in run["required_evidence"]
            ):
                return _pretool_result(
                    memory,
                    event,
                    action_kind=action_kind,
                    decision="deny",
                    reason="Comeback has no signed checkpoint capability for this session.",
                )
            if not exact_checkpoint:
                return _pretool_result(
                    memory,
                    event,
                    action_kind=action_kind,
                    decision="deny",
                    reason=(
                        "Comeback requires its exact signed checkpoint capability. Run exactly: "
                        + expected_checkpoint
                    ),
                )
            return _pretool_result(
                memory,
                event,
                action_kind=action_kind,
                decision="allow",
                reason=f"Comeback {run['mode']}: exact checkpoint capability allowed.",
            )
        if comeback_action not in {None, "release"}:
            return _pretool_result(
                memory,
                event,
                action_kind=action_kind,
                decision="deny",
                reason="Comeback refuses combined or ambiguous capability invocations.",
            )
        missing = memory.missing_requirements(run)
        if missing:
            return _pretool_result(
                memory,
                event,
                action_kind=action_kind,
                decision="deny",
                reason=(
                    f"Comeback {run['mode']}: remembered intervention requires "
                    + ", ".join(missing)
                    + " before this release action."
                ),
            )
        if run["lesson_ids"] and not exact_release:
            return _pretool_result(
                memory,
                event,
                action_kind=action_kind,
                decision="deny",
                reason=(
                    "Comeback requires its signed, one-shot release capability. Run exactly: "
                    + expected_release
                ),
            )
        return _pretool_result(
            memory,
            event,
            action_kind=action_kind,
            decision="allow",
            reason=(
                f"Comeback {run['mode']}: signed release capability requirements satisfied."
                if run["lesson_ids"]
                else "Comeback AUTONOMOUS: no remembered intervention applies to this action."
            ),
        )

    if event_name == "Stop":
        try:
            run = memory.get_run(session_id)
            _verify_run_principal(run, event, memory)
        except MemoryIntegrityError as exc:
            # Absence is not evidence that this session never needed
            # supervision: the run could have been deleted or recall never
            # activated. A fresh initialized low-risk run still stops normally.
            return _block_stop(
                f"Comeback fail-closed: {exc}. Start a genuinely fresh session; "
                "if recall does not activate, run comeback doctor before continuing.",
                event,
            )
        if run["status"] in {"completed", "failed"}:
            return None
        if run["status"] in {"executing", "unknown"}:
            return _block_stop(
                (
                    f"Comeback cannot finish: release outcome is {run['status']}; "
                    "operator reconciliation is required."
                ),
                event,
            )
        try:
            run = memory.get_verified_run(session_id)
            _verify_run_principal(run, event, memory)
        except MemoryIntegrityError as exc:
            return _block_stop(f"Comeback fail-closed: {exc}", event)
        if (
            run["status"] == "open"
            and run["lesson_ids"]
            and run["task_class"] == "release"
        ):
            reason = "Comeback cannot finish: " + (
                ", ".join(memory.missing_requirements(run))
                or "the supervised release action has not completed"
            )
            return _block_stop(reason, event)
    return None


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    cwd = event.get("cwd") or os.getcwd()
    repository = repository_configuration(str(cwd))
    root, repo_id = repository.root, repository.repo_id
    database = _database(root, event)
    # Every capability instruction carries the same absolute database chosen
    # for this lifecycle event. The agent cannot silently switch to the CLI's
    # default store between recall and enforcement.
    event["_comeback_memory_db"] = str(database)
    with InterventionMemory(
        database,
        repo_id,
        base_trust=repository.base_trust,
    ) as memory:
        return _handle_event(event, root=root, memory=memory)


def main() -> None:
    event = None
    try:
        # Read the lifecycle before parsing launcher options, so a damaged
        # installed command cannot erase the event's blocking response shape.
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise MemoryIntegrityError("hook input must be an object")
        if event.get("hook_event_name") not in {
            "PreToolUse", "UserPromptSubmit", "Stop", "PostToolUse"
        }:
            raise MemoryIntegrityError("hook input has no supported lifecycle event")
        agent_family = None
        cli_executable = None
        arguments = list(sys.argv[1:])
        while arguments:
            option = arguments.pop(0)
            if option not in {"--agent-family", "--cli-executable"} or not arguments:
                raise MemoryIntegrityError(
                    "usage: comeback-hook [--agent-family NAME] [--cli-executable PATH]"
                )
            value = arguments.pop(0)
            if not value:
                raise MemoryIntegrityError(f"{option} requires a value")
            if option == "--agent-family":
                agent_family = value
            else:
                cli_executable = value
        # Lifecycle JSON is untrusted. Only command-line values installed in
        # the reviewed hook launcher may populate private Comeback fields.
        event.pop("_comeback_agent_family", None)
        event.pop("_comeback_cli_executable", None)
        event.pop("_comeback_memory_db", None)
        if agent_family:
            event["_comeback_agent_family"] = agent_family
        if cli_executable:
            event["_comeback_cli_executable"] = cli_executable
        output = handle(event)
        if output is not None:
            print(json.dumps(output, sort_keys=True))
    except Exception as exc:
        event_name = event.get("hook_event_name") if isinstance(event, dict) else None
        if event_name == "PreToolUse":
            print(json.dumps(_deny(f"Comeback fail-closed: {exc}"), sort_keys=True))
            return
        if event_name == "Stop":
            print(
                json.dumps(
                    _block_stop(f"Comeback fail-closed: {exc}", event),
                    sort_keys=True,
                )
            )
            return
        if event_name == "UserPromptSubmit":
            print(json.dumps({"decision": "block", "reason": f"Comeback fail-closed: {exc}"}, sort_keys=True))
            return
        # Both supported harnesses use exit 2 to block a command hook when no
        # valid event-specific JSON can be produced. Never silently exit 0 on
        # unreadable input or emit a non-blocking informational message instead.
        print(f"Comeback fail-closed: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
