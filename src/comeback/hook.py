from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .identity import repository_identity
from .memory import InterventionMemory, MemoryIntegrityError
from .policy import classify_task, is_release_action, is_release_check, tool_succeeded


def _database(root: Path) -> Path:
    configured = os.environ.get("COMEBACK_MEMORY_DB")
    return Path(configured).expanduser().resolve() if configured else root / ".comeback" / "memory.db"


def _agent_family(_: dict[str, Any]) -> str:
    return "Codex"


def _context(run: dict[str, Any]) -> str:
    lessons = len(run["lesson_ids"])
    requirements = ", ".join(run["required_evidence"]) or "none"
    return (
        f"Comeback supervision: {run['mode']}. "
        f"Recalled intervention lessons: {lessons}. Required evidence: {requirements}. "
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


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    cwd = event.get("cwd") or os.getcwd()
    root, repo_id = repository_identity(str(cwd))
    memory = InterventionMemory(_database(root), repo_id)
    session_id = str(event.get("session_id", ""))
    event_name = event.get("hook_event_name")

    if not session_id:
        raise MemoryIntegrityError("hook event has no session identity")

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
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _context(run),
            }
        }

    if event_name == "PreToolUse" and is_release_action(event):
        try:
            run = memory.get_run(session_id)
        except MemoryIntegrityError as exc:
            return _deny(f"Comeback fail-closed: {exc}")
        if run["task_class"] != "release":
            return _deny("Comeback blocked a release action outside a release-class supervision run.")
        missing = memory.missing_requirements(run)
        if missing:
            return _deny(
                f"Comeback {run['mode']}: remembered intervention requires "
                + ", ".join(missing)
                + " before this release action."
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Comeback {run['mode']}: release gate satisfied.",
            }
        }

    if event_name == "PostToolUse" and is_release_check(event):
        if tool_succeeded(event):
            run = memory.add_evidence(session_id, "release_check_passed")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "Comeback recorded release_check_passed in Sibyl. "
                    f"Remaining: {', '.join(memory.missing_requirements(run)) or 'none'}.",
                }
            }
        return {
            "decision": "block",
            "reason": "Comeback observed a failed release check. Repair it before release.",
        }

    if event_name == "PostToolUse" and is_release_action(event):
        run = memory.record_release_outcome(session_id, success=tool_succeeded(event))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"Comeback stored release outcome: {run['outcome']}.",
            }
        }

    if event_name == "Stop":
        try:
            run = memory.get_run(session_id)
        except MemoryIntegrityError:
            return None
        if run["status"] == "open" and run["mode"] != "AUTONOMOUS":
            if event.get("stop_hook_active"):
                return {
                    "continue": False,
                    "systemMessage": "Comeback left this run open; operator action is still required.",
                }
            return {
                "decision": "block",
                "reason": "Comeback cannot finish: "
                + (", ".join(memory.missing_requirements(run)) or "the supervised release action has not completed"),
            }
    return None


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise MemoryIntegrityError("hook input must be an object")
        output = handle(event)
        if output is not None:
            print(json.dumps(output, sort_keys=True))
    except Exception as exc:
        event_name = locals().get("event", {}).get("hook_event_name") if isinstance(locals().get("event"), dict) else None
        if event_name == "PreToolUse":
            print(json.dumps(_deny(f"Comeback fail-closed: {exc}"), sort_keys=True))
            return
        print(json.dumps({"systemMessage": f"Comeback hook error: {exc}"}, sort_keys=True))


if __name__ == "__main__":
    main()
