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
_SHELLS = {"bash", "dash", "sh", "zsh"}
_COMMAND_SEPARATORS = {"&&", "||", ";", "|"}


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
        words = _shell_words(command_from_event(event))
    except ValueError:
        return False
    return _contains_release_action(words)


def _shell_words(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _clean_word(word: str) -> str:
    return PurePath(word.strip("(){};")).name.lower()


def _command_segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for word in words:
        if word in _COMMAND_SEPARATORS:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(word)
    return [segment for segment in segments if segment]


def _contains_release_action(words: list[str]) -> bool:
    return any(_segment_is_release(segment) for segment in _command_segments(words))


def _segment_is_release(words: list[str]) -> bool:
    if not words:
        return False
    normalized = [_clean_word(word) for word in words]
    index = 0

    while index < len(normalized) and "=" in words[index] and not words[index].startswith("="):
        index += 1
    if index >= len(normalized):
        return False

    executable = normalized[index]
    if executable in {"command", "sudo"}:
        index += 1
        while index < len(normalized) and words[index].startswith("-"):
            index += 1
        return _segment_is_release(words[index:])
    if executable == "env":
        index += 1
        while index < len(normalized) and (
            words[index].startswith("-") or "=" in words[index]
        ):
            index += 1
        return _segment_is_release(words[index:])
    if executable in _SHELLS:
        try:
            command_index = normalized.index("-c", index + 1) + 1
        except (ValueError, IndexError):
            return False
        try:
            return _contains_release_action(_shell_words(words[command_index]))
        except ValueError:
            return False
    if executable == "eval" and index + 1 < len(words):
        try:
            return _contains_release_action(_shell_words(" ".join(words[index + 1 :])))
        except ValueError:
            return False

    tail = normalized[index + 1 :]
    if executable == "git" and _git_arguments(tail)[:1] == ["push"]:
        return True
    if executable == "gh" and tail[:2] == ["pr", "merge"]:
        return True
    if executable == "wrangler" and tail[:1] == ["deploy"]:
        return True
    if executable == "vercel" and "--prod" in tail:
        return True
    if executable in _PACKAGE_MANAGERS:
        arguments = [word for word in tail if not word.startswith("-")]
        if arguments[:1] == ["publish"]:
            return True
        if len(arguments) >= 2 and arguments[0] == "run" and arguments[1] in _RELEASE_SCRIPTS:
            return True
    if executable == "forge" and tail[:1] == ["script"] and "--broadcast" in tail:
        return True
    if executable.startswith("python") and tail:
        script = next((word for word in tail if not word.startswith("-")), "")
        return script in {"release_candidate.py", "release-candidate.py"}
    return executable in {"release_candidate.py", "release-candidate.py"}


def _git_arguments(arguments: list[str]) -> list[str]:
    index = 0
    options_with_values = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
    while index < len(arguments):
        argument = arguments[index]
        if argument in options_with_values:
            index += 2
            continue
        if argument.startswith(("--git-dir=", "--work-tree=", "--namespace=")):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        break
    return arguments[index:]


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
