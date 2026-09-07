"""Native-terminal correction capture; never infer a command from its hash."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from .memory import InterventionMemory, MemoryIntegrityError, _signed_intervention_fields, utc_now
from .owner import owner_address, sign_with_owner
from .signing import intervention_message


def _ask(label: str, *, allow_empty: bool = False) -> str:
    print(label, end=" ", file=sys.stderr, flush=True)
    try:
        value = input()
    except (EOFError, KeyboardInterrupt) as exc:
        raise MemoryIntegrityError("Capture cancelled; no correction saved") from exc
    if value == ":cancel":
        raise MemoryIntegrityError("Capture cancelled; no correction saved")
    if not allow_empty and not value.strip():
        raise MemoryIntegrityError("A value is required; no correction saved")
    return value


def _argv(label: str) -> list[str]:
    print(f"{label}: enter the executable, then one argument per line. "
          "Do not add shell quotes. Blank argument ends the list; :cancel aborts.", file=sys.stderr)
    result = [_ask("Executable:")]
    for index in range(1, 65):
        value = _ask(f"Argument {index} (blank to finish):", allow_empty=True)
        if value == "":
            return result
        result.append(value)
    raise MemoryIntegrityError("Too many arguments; no correction saved")


def capture_correction(
    memory: InterventionMemory, *, repo_id: str, keystore: Path,
    session_id: str | None, confirm: Callable[[str, dict], None],
) -> dict:
    if not sys.stdin.isatty():
        raise MemoryIntegrityError("Run `comeback capture` yourself in a native terminal, not a pipe or agent tool")
    closer = owner_address(keystore)
    print("Capture a missed release or migration check. Nothing is executed. "
          "Commands must be supplied by you; stored hashes cannot reconstruct them.", file=sys.stderr)
    if session_id is None:
        runs = [run for run in memory.list_runs() if run["task_class"] == "release"]
        if not runs:
            raise MemoryIntegrityError("No release/migration session found. Run doctor, then start a real working agent task; use status for its exact session ID")
        for run in runs:
            print(json.dumps({key: run.get(key) for key in
                              ("session_id", "agent_family", "area", "started_at", "status")}), file=sys.stderr)
        session_id = _ask("Exact corrected session ID (no automatic latest selection):")
    source = memory.get_run(session_id)
    if source["task_class"] != "release":
        raise MemoryIntegrityError("Only release and migration corrections are supported; selected session is not a release task")
    print(json.dumps({"selected_session": session_id, "workflow": source["area"],
                      "agent": source["agent_family"],
                      "observed_decisions": memory.pretool_decisions(session_id)}, indent=2), file=sys.stderr)
    summary = _ask("What check did the agent miss? (Your report, not automatically verified):")
    scope = _ask("Apply to this agent only or both supported agents? Enter same_agent or all_supported:")
    if scope not in {"same_agent", "all_supported"}:
        raise MemoryIntegrityError("Choose same_agent or all_supported; no correction saved")
    checkpoint = _argv("Mandatory check")
    release = _argv("Protected action (Git push needs a direct URL/absolute bare path and explicit refspec)")
    fields = {
        "lesson_id": f"{source['task_class']}-{source['area']}-{source['agent_family'].lower()}",
        "repo_id": repo_id, "task_class": source["task_class"], "area": source["area"],
        "agent_family": source["agent_family"], "agent_scope": scope,
        "severity": "release_blocker", "action_schema": 2,
        "checkpoint_spec": {"argv": checkpoint, "timeout_seconds": 600},
        "release_spec": {"argv": release, "timeout_seconds": 600},
        "state_policy": {"bind_head": True, "require_clean_git": True},
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": closer, "source_session_id": session_id, "incident_at": utc_now(),
    }
    _signed_intervention_fields(fields)  # Reject unsupported commands before password/signing.
    confirm("SIGN", {"signed_fields": fields, "owner_report": summary,
                     "effect": "This workflow must always pass the check. First release needs owner approval; success removes repeat approval only."})
    signature = sign_with_owner(keystore, intervention_message(fields))
    return memory.record_intervention({"signed_fields": fields,
                                      "intervention_signature": signature,
                                      "incident_summary": summary})


def explain_session(memory: InterventionMemory, session_id: str) -> dict:
    run = memory.get_verified_run(session_id)
    remaining = memory.missing_requirements(run)
    return {
        "session_id": session_id, "workflow": run["area"], "mode": run["mode"],
        "status": run["status"], "remembered_requirements_from": run["lesson_ids"],
        "mandatory_check": run.get("checkpoint_spec"),
        "recorded_evidence_still_missing": remaining,
        "why": ("A remembered correction keeps this verifier mandatory. "
                + ("Owner approval is still required." if "human_approval" in run["required_evidence"]
                   else "Successful history removed repeat approval, not the verifier."))
               if run["lesson_ids"] else "No matching correction; ordinary repository checks still apply.",
        "next": ("This run is closed; start a fresh task." if run["status"] != "open" else
                 "Run the checkpoint capability, then obtain owner approval if required."
                 if "release_check_passed" in remaining else
                 "Ask the owner to review and approve this exact session in their terminal."
                 if "human_approval" in remaining else
                 "Use the release capability if supervised; it will revalidate before execution."),
        "notice": "Read-only memory explanation, not release authorization. Evidence age, repository state and execution locks are rechecked at release time.",
    }
