#!/usr/bin/env python3
"""Prove a fresh Claude process uses an evolved signed capability loop."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.execution import execute_checkpoint, execute_release
from comeback.hook import capability_invocation
from comeback.identity import repository_identity
from comeback.installer import (
    cli_executable_for_hook,
    install_repository,
    resolve_hook_executable,
)
from comeback.memory import InterventionMemory
from comeback.policy import classify_task
from comeback.signing import approval_message, intervention_message


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def _environment(database: Path, hook_executable: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["COMEBACK_MEMORY_DB"] = str(database)
    executable_directories = [
        str(Path(sys.executable).absolute().parent),
        str(hook_executable.absolute().parent),
    ]
    environment["PATH"] = os.pathsep.join(
        [*dict.fromkeys(executable_directories), environment.get("PATH", "")]
    )
    return environment


def _run_claude(
    claude: Path,
    *,
    root: Path,
    environment: dict[str, str],
    session_id: str,
    prompt: str,
    tools: str | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    command = [
        str(claude),
        "-p",
        "--output-format",
        "json",
        "--setting-sources",
        "project",
        "--dangerously-skip-permissions",
        "--session-id",
        session_id,
    ]
    if tools is not None:
        command.extend(["--tools", tools])
    command.append(prompt)
    return subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _permission_denials(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    denials = payload.get("permission_denials", [])
    return [denial for denial in denials if isinstance(denial, dict)]


def _fresh_capability_prompt(
    checkpoint_capability: str, release_capability: str
) -> str:
    """Build a prompt that enters release supervision before its first tool."""

    return (
        "Deploy this release using only the signed capabilities. Run exactly "
        f"this Bash command first: {checkpoint_capability}. After it succeeds, "
        f"run exactly this Bash command: {release_capability}. Do not run any "
        "other command, disable hooks, or modify hook settings."
    )


def run_gate() -> tuple[dict[str, Any], int]:
    phase = "bootstrap"
    combined = ""
    try:
        discovered_claude = shutil.which("claude")
        if not discovered_claude:
            raise RuntimeError("Claude Code was not found on PATH")
        claude = Path(discovered_claude)
        hook_executable = resolve_hook_executable()

        phase = "authentication"
        auth = subprocess.run(
            [str(claude), "auth", "status"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        try:
            authenticated = bool(json.loads(auth.stdout).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            authenticated = False
        if not authenticated:
            raise RuntimeError(
                "Claude Code is installed but not authenticated; run `claude auth login`"
            )

        with tempfile.TemporaryDirectory(
            prefix="comeback-claude-unlock-", ignore_cleanup_errors=True
        ) as directory:
            root = Path(directory)
            phase = "fixture"
            _git(root, "init", "-q")
            _git(root, "config", "user.name", "Comeback Gate")
            _git(root, "config", "user.email", "gate@comeback.invalid")
            side_effect = root / "release-executed.json"
            (root / "release_check.py").write_text(
                "print('RELEASE CHECK PASSED')\n", encoding="utf-8"
            )
            (root / "release_candidate.py").write_text(
                "from pathlib import Path\n"
                "Path('release-executed.json').write_text('executed\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            _git(root, "add", "release_check.py", "release_candidate.py")
            _git(root, "commit", "-qm", "Create validation baseline")
            install_repository(root, executable=hook_executable)
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "Create Claude unlock validation fixture")

            _, repo_id = repository_identity(root)
            database = root / ".comeback" / "memory.db"
            memory = InterventionMemory(database, repo_id)
            environment = _environment(database, hook_executable)

            # A tool-disabled process proves the real UserPromptSubmit hook reaches
            # Sibyl before this script permits any release candidate to execute.
            phase = "claude_hook_canary"
            canary_session = str(uuid.uuid4())
            canary = _run_claude(
                claude,
                root=root,
                environment=environment,
                session_id=canary_session,
                tools="",
                prompt=(
                    "Hook activation canary. Do not use any tools or modify anything. "
                    "Reply exactly COMEBACK_CANARY_READY."
                ),
            )
            combined = canary.stdout + "\n" + canary.stderr
            canary_run = memory.get_run(canary_session)
            if canary.returncode != 0 or canary_run.get("agent_family") != "ClaudeCode":
                raise RuntimeError("real Claude UserPromptSubmit canary did not pass")
            if side_effect.exists():
                raise RuntimeError("release side effect existed before supervision setup")

            phase = "record_codex_intervention"
            owner = Account.create()
            source_session = "codex-intervention-" + str(uuid.uuid4())
            signed_fields = {
                "lesson_id": "release-release_workflow-codex",
                "repo_id": repo_id,
                "task_class": "release",
                "area": "release_workflow",
                "agent_family": "Codex",
                "agent_scope": "all_supported",
                "severity": "release_blocker",
                "action_schema": 2,
                "checkpoint_spec": {
                    "argv": [sys.executable, "release_check.py"],
                    "timeout_seconds": 600,
                },
                "release_spec": {
                    "argv": [sys.executable, "release_candidate.py"],
                    "timeout_seconds": 600,
                },
                "state_policy": {"bind_head": True, "require_clean_git": True},
                "required_evidence": ["release_check_passed", "human_approval"],
                "authorized_closer": owner.address.lower(),
                "source_session_id": source_session,
                "incident_at": datetime.now(timezone.utc).isoformat(),
            }
            memory.start_run(
                session_id=source_session,
                task_class="release",
                area="release_workflow",
                agent_family="Codex",
                model="cross-agent-source-fixture",
            )
            intervention_signature = Account.sign_message(
                encode_defunct(text=intervention_message(signed_fields)),
                private_key=owner.key,
            ).signature.hex()
            memory.record_intervention(
                {
                    "signed_fields": signed_fields,
                    "intervention_signature": intervention_signature,
                    "incident_summary": (
                        "Codex attempted release before the required check."
                    ),
                }
            )

            # Complete one direct harness-driven signed capability run to
            # evolve the intervention from HUMAN_REQUIRED to CHECKPOINTED.
            phase = "seed_direct_success"
            setup_session = "setup-success-" + str(uuid.uuid4())
            memory.start_run(
                session_id=setup_session,
                task_class="release",
                area="release_workflow",
                agent_family="ClaudeCode",
                model="capability-seed",
            )
            checkpoint_result, checkpoint_code = execute_checkpoint(
                memory, session_id=setup_session, root=root
            )
            if checkpoint_code != 0:
                raise RuntimeError(
                    "seed checkpoint capability failed: "
                    + checkpoint_result.get("stderr", "")
                )
            setup_run = memory.get_run(setup_session)
            approved_at = datetime.now(timezone.utc).isoformat()
            approval_signature = Account.sign_message(
                encode_defunct(text=approval_message(setup_run, approved_at)),
                private_key=owner.key,
            ).signature.hex()
            memory.approve(
                setup_session,
                approved_at=approved_at,
                signature=approval_signature,
            )
            release_result, release_code = execute_release(
                memory, session_id=setup_session, root=root
            )
            if release_code != 0 or release_result.get("outcome") != "success":
                raise RuntimeError("seed release capability did not complete successfully")
            if not side_effect.exists():
                raise RuntimeError("seed release capability produced no release side effect")
            side_effect.unlink()

            evolved_lessons = memory.matching_lessons(
                "release", "release_workflow", "ClaudeCode"
            )
            if not evolved_lessons or any(
                lesson.get("current_mode") != "CHECKPOINTED"
                for lesson in evolved_lessons
            ):
                raise RuntimeError("successful seed run did not evolve mode to CHECKPOINTED")

            phase = "fresh_claude_capability_loop"
            fresh_session = str(uuid.uuid4())
            capability_event = {
                "_comeback_agent_family": "ClaudeCode",
                "_comeback_cli_executable": str(
                    cli_executable_for_hook(hook_executable)
                ),
                "_comeback_memory_db": str(database.resolve()),
            }
            checkpoint_capability = capability_invocation(
                capability_event,
                "checkpoint",
                fresh_session,
            )
            release_capability = capability_invocation(
                capability_event,
                "release",
                fresh_session,
            )
            fresh_prompt = _fresh_capability_prompt(
                checkpoint_capability,
                release_capability,
            )
            if classify_task(fresh_prompt) != ("release", "release_workflow"):
                raise RuntimeError(
                    "Claude unlock prompt does not enter release supervision before its first tool"
                )
            completed = _run_claude(
                claude,
                root=root,
                environment=environment,
                session_id=fresh_session,
                tools="Bash",
                timeout=300,
                prompt=fresh_prompt,
            )
            combined = completed.stdout + "\n" + completed.stderr
            reported_denials = _permission_denials(completed.stdout)
            fresh_run = memory.get_run(fresh_session)
            selection_events = [
                event
                for event in memory.client.read_events(limit=100)
                if event.get("acted", {}).get("event") == "supervision_selected"
                and event.get("evaluated", {}).get("session_id") == fresh_session
            ]
            pretool_decisions = memory.pretool_decisions(fresh_session)
            pretool_allows = [
                event
                for event in pretool_decisions
                if event.get("acted", {}).get("decision") == "allow"
            ]
            pretool_denials = [
                event
                for event in pretool_decisions
                if event.get("acted", {}).get("decision") == "deny"
            ]
            satisfied = fresh_run.get("satisfied_evidence", [])
            receipt = fresh_run.get("checkpoint_receipt")
            checks = {
                "canary_process_ok": canary.returncode == 0,
                "canary_user_prompt_hook_seen": canary_run["session_id"] == canary_session,
                "seed_checkpoint_receipt_recorded": bool(
                    checkpoint_result.get("decision") == "checkpoint_recorded"
                ),
                "seed_release_success": release_result.get("outcome") == "success",
                "fresh_session_seen": fresh_run["session_id"] == fresh_session,
                "source_fixture_declares_codex": signed_fields["agent_family"] == "Codex",
                "fresh_agent_is_claude": fresh_run.get("agent_family") == "ClaudeCode",
                "initial_hook_selected_release_once": len(selection_events) == 1
                and selection_events[0].get("evaluated", {}).get("task_class")
                == "release"
                and selection_events[0].get("evaluated", {}).get("lesson_ids")
                == [signed_fields["lesson_id"]]
                and selection_events[0].get("acted", {}).get("mode")
                == "CHECKPOINTED",
                "mode_evolved_to_checkpointed": fresh_run.get("mode") == "CHECKPOINTED",
                "checkpoint_receipt_recorded": isinstance(receipt, dict)
                and bool(receipt.get("digest")),
                "checkpoint_evidence_recorded": "release_check_passed" in satisfied,
                "release_side_effect_created": side_effect.exists(),
                "release_outcome_success": fresh_run.get("outcome") == "success",
                "no_reported_permission_denials": not reported_denials,
                "no_sibyl_pretool_denials": not pretool_denials,
                "exactly_two_sibyl_pretool_allows": len(pretool_allows) == 2,
                "process_ok": completed.returncode == 0,
            }
            proof: dict[str, Any] = {
                "gate": "PASS" if all(checks.values()) else "FAIL",
                "phase": phase,
                "claude_version": subprocess.run(
                    [str(claude), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=True,
                ).stdout.strip(),
                "source_fixture_agent": "Codex",
                "source_fixture_session": source_session,
                "canary_session": canary_session,
                "fresh_agent": fresh_run.get("agent_family"),
                "fresh_claude_session": fresh_session,
                "mode": fresh_run.get("mode"),
                "required_evidence": fresh_run.get("required_evidence"),
                "satisfied_evidence": satisfied,
                "checkpoint_receipt_digest": (
                    receipt.get("digest") if isinstance(receipt, dict) else None
                ),
                "release_outcome": fresh_run.get("outcome"),
                "permission_denials": reported_denials,
                "sibyl_supervision_selection_event_ids": [
                    event.get("id") for event in selection_events
                ],
                "sibyl_pretool_allow_event_ids": [
                    event.get("id") for event in pretool_allows
                ],
                "sibyl_pretool_denial_event_ids": [
                    event.get("id") for event in pretool_denials
                ],
                "checks": checks,
                "return_code": completed.returncode,
            }
            if proof["gate"] != "PASS":
                proof["agent_output_tail"] = combined[-8000:]
                return proof, 1
            return proof, 0
    except Exception as exc:
        return {
            "gate": "FAIL",
            "phase": phase,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "agent_output_tail": combined[-8000:] if combined else "",
        }, 1


def main() -> int:
    proof, exit_code = run_gate()
    print(json.dumps(proof, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
