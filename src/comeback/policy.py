from __future__ import annotations

import re
import shlex
from pathlib import PurePath
from typing import Any


MODES = ("AUTONOMOUS", "CHECKPOINTED", "HUMAN_REQUIRED")

_RELEASE_PROMPT = re.compile(
    r"\b(release|deploy|deployment|publish|production|merge|git\s+push|ship)\b",
    re.IGNORECASE,
)
_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_RELEASE_SCRIPTS = {"deploy", "release", "publish"}


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
    try:
        words = shlex.split(command_from_event(event), posix=True)
    except ValueError:
        return False
    commands = [PurePath(word.strip("(){};")).name.lower() for word in words]
    for index, executable in enumerate(commands):
        tail = commands[index + 1 :]
        if executable == "git" and tail[:1] == ["push"]:
            return True
        if executable == "gh" and tail[:2] == ["pr", "merge"]:
            return True
        if executable == "wrangler" and tail[:1] == ["deploy"]:
            return True
        if executable == "vercel" and "--prod" in tail:
            return True
        if executable in _PACKAGE_MANAGERS:
            if tail[:1] == ["publish"]:
                return True
            if len(tail) >= 2 and tail[0] == "run" and tail[1] in _RELEASE_SCRIPTS:
                return True
        if executable == "forge" and tail[:1] == ["script"] and "--broadcast" in tail:
            return True
        if executable in {"release_candidate.py", "release-candidate.py"}:
            return True
    return False


def _exit_codes(value: Any) -> list[int]:
    codes: list[int] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("exit_code", "exitCode") and isinstance(item, int):
                codes.append(item)
            else:
                codes.extend(_exit_codes(item))
    elif isinstance(value, list):
        for item in value:
            codes.extend(_exit_codes(item))
    return codes


def _response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_response_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_response_text(item) for item in value)
    return ""


def checkpoint_invocation(command: str, success_marker: str) -> str:
    command = command.strip()
    if not success_marker:
        return command
    return f"({command}) && printf '\\n{success_marker}\\n'"


def is_success_wrapped(command: str, success_marker: str) -> bool:
    if not success_marker:
        return True
    suffix = f") && printf '\\n{success_marker}\\n'"
    return command.strip().startswith("(") and command.strip().endswith(suffix)


def tool_succeeded(event: dict[str, Any], *, expected_marker: str = "") -> bool:
    response = event.get("tool_response")
    codes = _exit_codes(response)
    if codes:
        return all(code == 0 for code in codes)
    if isinstance(response, dict) and isinstance(response.get("success"), bool):
        return response["success"]
    text = _response_text(response)
    if expected_marker:
        return expected_marker in text
    text = text.lower()
    return bool(
        re.search(r"\bexit(?:ed)?(?:\s+with)?\s+code\s*[:=]?\s*0\b", text)
        or re.search(r"\bprocess\s+exited\s+successfully\b", text)
        or "release check passed" in text
    )


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
