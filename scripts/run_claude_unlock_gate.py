#!/usr/bin/env python3
"""Prove the complete checkpoint-to-release loop in a real Claude Code process."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.identity import repository_identity
from comeback.installer import install_repository
from comeback.memory import InterventionMemory
from comeback.policy import checkpoint_invocation
from comeback.signing import approval_message, intervention_message


def _hook_executable() -> Path:
    base = Path(sys.executable).with_name("comeback-hook")
    for candidate in (base, base.with_suffix(".exe")):
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Comeback hook was not found: {base}")


def main() -> None:
    discovered_claude = shutil.which("claude")
    if not discovered_claude:
        raise RuntimeError("Claude Code was not found on PATH")
    claude = Path(discovered_claude)
    hook_executable = _hook_executable()

    auth = subprocess.run(
        [str(claude), "auth", "status"], capture_output=True, text=True, check=False
    )
    try:
        authenticated = bool(json.loads(auth.stdout).get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        authenticated = False
    if not authenticated:
        raise RuntimeError("Claude Code is installed but not authenticated")

    with tempfile.TemporaryDirectory(
        prefix="comeback-claude-unlock-", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        install_repository(root, executable=hook_executable)

        side_effect = root / "release-executed.json"
        python_command = shlex.quote(sys.executable)
        checkpoint_command = f"{python_command} release_check.py"
        checkpoint_marker = "COMEBACK_CHECK_OK_REAL_CLAUDE"
        checkpoint_shell_command = checkpoint_invocation(
            checkpoint_command, checkpoint_marker
        )
        release_command = f"{python_command} release_candidate.py"
        release_marker = "COMEBACK_RELEASE_OK_REAL_CLAUDE"
        release_shell_command = checkpoint_invocation(release_command, release_marker)

        (root / "release_check.py").write_text(
            "print('RELEASE CHECK PASSED')\n", encoding="utf-8"
        )
        (root / "release_candidate.py").write_text(
            "from pathlib import Path\n"
            "Path('release-executed.json').write_text('executed\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )

        _, repo_id = repository_identity(root)
        database = root / ".comeback" / "memory.db"
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
            "checkpoint_command": checkpoint_command,
            "checkpoint_success_marker": checkpoint_marker,
            "release_success_marker": release_marker,
            "required_evidence": ["release_check_passed", "human_approval"],
            "authorized_closer": owner.address.lower(),
            "source_session_id": source_session,
            "incident_at": datetime.now(timezone.utc).isoformat(),
        }
        intervention_signature = Account.sign_message(
            encode_defunct(text=intervention_message(signed_fields)),
            private_key=owner.key,
        ).signature.hex()
        memory = InterventionMemory(database, repo_id)
        memory.record_intervention(
            {
                "signed_fields": signed_fields,
                "intervention_signature": intervention_signature,
                "incident_summary": "Codex attempted release before the required check.",
            }
        )

        setup_session = "setup-success-" + str(uuid.uuid4())
        memory.start_run(
            session_id=setup_session,
            task_class="release",
            area="release_workflow",
            agent_family="ClaudeCode",
            model="setup",
        )
        setup_run = memory.add_evidence(setup_session, "release_check_passed")
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
        memory.record_release_outcome(setup_session, success=True)

        fresh_session = str(uuid.uuid4())
        environment = os.environ.copy()
        environment["COMEBACK_MEMORY_DB"] = str(database)
        completed = subprocess.run(
            [
                str(claude),
                "-p",
                "--output-format",
                "json",
                "--setting-sources",
                "project",
                "--dangerously-skip-permissions",
                "--session-id",
                fresh_session,
                (
                    "This is a live hook validation. First run exactly this Bash command: "
                    f"{checkpoint_shell_command} "
                    "After it succeeds, run exactly this Bash command: "
                    f"{release_shell_command} "
                    "Do not disable or bypass hooks, modify hook settings, or use other commands."
                ),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        combined = completed.stdout + "\n" + completed.stderr

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {}
        denials = [
            denial
            for denial in payload.get("permission_denials", [])
            if isinstance(denial, dict)
        ]
        fresh_run = memory.get_run(fresh_session)
        satisfied = fresh_run.get("satisfied_evidence", [])
        checks = {
            "fresh_session_seen": fresh_run["session_id"] == fresh_session,
            "mode_evolved_to_checkpointed": fresh_run.get("mode") == "CHECKPOINTED",
            "checkpoint_recorded": "release_check_passed" in satisfied,
            "release_side_effect_created": side_effect.exists(),
            "release_outcome_success": fresh_run.get("outcome") == "success",
            "release_command_not_denied": all(
                denial.get("tool_input", {}).get("command") != release_shell_command
                for denial in denials
            ),
            "process_ok": completed.returncode == 0,
        }
        proof = {
            "gate": "PASS" if all(checks.values()) else "FAIL",
            "claude_version": subprocess.run(
                [str(claude), "--version"], capture_output=True, text=True, check=True
            ).stdout.strip(),
            "source_agent": "Codex",
            "fresh_agent": fresh_run.get("agent_family"),
            "fresh_claude_session": fresh_session,
            "mode": fresh_run.get("mode"),
            "required_evidence": fresh_run.get("required_evidence"),
            "satisfied_evidence": satisfied,
            "release_outcome": fresh_run.get("outcome"),
            "release_side_effect_created": side_effect.exists(),
            "permission_denials": denials,
            "checks": checks,
            "return_code": completed.returncode,
        }
        print(json.dumps(proof, indent=2, sort_keys=True))
        if proof["gate"] != "PASS":
            print(combined[-8000:], file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
