from __future__ import annotations

import re
from typing import Any


MODES = ("AUTONOMOUS", "CHECKPOINTED", "HUMAN_REQUIRED")

_RELEASE_PROMPT = re.compile(
    r"\b(release|deploy|deployment|publish|production|merge|git\s+push|ship)\b",
    re.IGNORECASE,
)
_RELEASE_ACTIONS = (
    re.compile(r"(^|[;&|]\s*)git\s+push\b", re.IGNORECASE),
    re.compile(r"\bgh\s+pr\s+merge\b", re.IGNORECASE),
    re.compile(r"\bwrangler\s+deploy\b", re.IGNORECASE),
    re.compile(r"\bvercel\b.*\s--prod\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+publish\b", re.IGNORECASE),
    re.compile(r"\bforge\s+script\b.*\s--broadcast\b", re.IGNORECASE),
    re.compile(r"\brelease_candidate\.py\b", re.IGNORECASE),
)
_RELEASE_CHECK = re.compile(r"\brelease_check\.py\b", re.IGNORECASE)


def classify_task(prompt: str) -> tuple[str, str]:
    if _RELEASE_PROMPT.search(prompt):
        return "release", "release_workflow"
    return "low_risk", "general"


def command_from_event(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def is_release_action(event: dict[str, Any]) -> bool:
    if event.get("tool_name") != "Bash":
        return False
    command = command_from_event(event)
    return any(pattern.search(command) for pattern in _RELEASE_ACTIONS)


def is_release_check(event: dict[str, Any]) -> bool:
    return event.get("tool_name") == "Bash" and bool(
        _RELEASE_CHECK.search(command_from_event(event))
    )


def tool_succeeded(event: dict[str, Any]) -> bool:
    response = event.get("tool_response")
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode"):
            if key in response:
                return response[key] == 0
        content = response.get("content")
        if isinstance(content, list):
            text = " ".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
            return "exit code 0" in text.lower() or "release check passed" in text.lower()
    return False


def mode_for_outcomes(failures: int, successes: int) -> str:
    if successes >= failures + 2:
        return "AUTONOMOUS"
    if successes >= failures:
        return "CHECKPOINTED"
    return "HUMAN_REQUIRED"


def requirements_for_mode(mode: str) -> list[str]:
    if mode == "HUMAN_REQUIRED":
        return ["release_check_passed", "human_approval"]
    if mode == "CHECKPOINTED":
        return ["release_check_passed"]
    return []

