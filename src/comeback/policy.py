from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePath
from typing import Any


MODES = ("AUTONOMOUS", "CHECKPOINTED", "HUMAN_REQUIRED")

_RELEASE_PROMPT_PATTERNS = (
    re.compile(r"\bgit\s+push\b", re.IGNORECASE),
    re.compile(r"\bgh\s+pr\s+merge\b", re.IGNORECASE),
    re.compile(r"\bmerge\s+(?:this\s+|the\s+)?(?:pr|pull\s+request)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:deploy|publish|release)\s+(?:this|it|the|to|on|into|our|my|a)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:deploy|publish|release)\s*[.!?]?\s*$", re.IGNORECASE),
    re.compile(r"\bship\s+(?:this|it|the\s+(?:release|build|app|site|service))\b", re.IGNORECASE),
)
_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_RELEASE_SCRIPTS = {"deploy", "release", "publish"}
_SHELLS = {"bash", "dash", "sh", "zsh"}
_COMMAND_SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}


def classify_task(prompt: str) -> tuple[str, str]:
    if any(pattern.search(prompt) for pattern in _RELEASE_PROMPT_PATTERNS):
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
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
    # Keep unquoted newlines as command separators while shlex still preserves
    # a newline inside quotes as part of the quoted argument.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _clean_word(word: str) -> str:
    cleaned = word.strip("(){};").replace("\\", "/")
    return PurePath(cleaned).name.lower()


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
    if executable in {"command", "exec", "sudo"}:
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
        command_option = next(
            (
                option_index
                for option_index in range(index + 1, len(normalized))
                if normalized[option_index] == "-c"
                or (
                    normalized[option_index].startswith("-")
                    and not normalized[option_index].startswith("--")
                    and "c" in normalized[option_index][1:]
                )
            ),
            None,
        )
        if command_option is None or command_option + 1 >= len(words):
            return False
        command_index = command_option + 1
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
    if executable in {"comeback", "comeback.exe"} and tail[:1] == ["release"]:
        return True
    if executable.startswith("python") and tail:
        script = next((word for word in tail if not word.startswith("-")), "")
        return script in {"release_candidate.py", "release-candidate.py"}
    return executable in {"release_candidate.py", "release-candidate.py"}


def is_release_capability(
    command: str,
    expected_invocation: str,
    *,
    working_directory: str | Path | None = None,
) -> bool:
    """Match only the exact trusted release launcher injected by Comeback.

    Parsing a command and accepting a basename such as ``comeback`` is unsafe:
    a repository can provide a counterfeit executable and shells evaluate command
    substitutions before the real program starts. Exact equality deliberately
    rejects alternate paths, extra flags, substitutions, redirections and command
    chains. A literal ``cd`` to the same repository is the sole accepted prefix.
    """

    return invocation_matches(
        command,
        expected_invocation,
        working_directory=working_directory,
    )


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


def _command_after_same_directory_prefix(
    command: str, working_directory: str | Path | None
) -> str | None:
    candidate = command.strip()
    match = re.fullmatch(
        r"(?:cd|set-location)\s+(.+?)\s*(?:&&|;)\s*(.+)",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return candidate
    if working_directory is None:
        return None
    raw_target = match.group(1).strip()
    if raw_target.lower().startswith("/d "):
        raw_target = raw_target[3:].strip()
    if raw_target.lower().startswith("-literalpath "):
        raw_target = raw_target[len("-literalpath ") :].strip()
    if (
        len(raw_target) >= 2
        and raw_target[0] in {"'", '"'}
        and raw_target[-1] == raw_target[0]
    ):
        raw_target = raw_target[1:-1]
    if not raw_target or raw_target[0] in {"$", "`"}:
        return None
    target = Path(raw_target).expanduser()
    if not target.is_absolute():
        target = Path(working_directory) / target
    try:
        if target.resolve() != Path(working_directory).resolve():
            return None
    except OSError:
        return None
    return match.group(2).strip()


def invocation_matches(
    command: str,
    expected_invocation: str,
    *,
    working_directory: str | Path | None = None,
) -> bool:
    candidate = _command_after_same_directory_prefix(command, working_directory)
    return candidate is not None and candidate == expected_invocation.strip()


def invokes_configured_argv(
    command: str,
    argv: list[str],
    *,
    working_directory: str | Path | None = None,
) -> bool:
    """Recognize the literal configured action vector across supported shells.

    This is deliberately an exact argv comparison, not equivalence analysis.
    It closes the important case where a configured executable is outside the
    built-in release vocabulary, while arbitrary wrappers remain outside the
    raw-command defense-in-depth boundary.
    """

    candidate = _command_after_same_directory_prefix(command, working_directory)
    if candidate is None:
        return False
    candidate = candidate.strip()
    if candidate.startswith("& "):
        candidate = candidate[2:].strip()
    renderings = {shlex.join(argv)}
    try:
        import subprocess

        renderings.add(subprocess.list2cmdline(argv))
    except (ImportError, ValueError):  # pragma: no cover - stdlib/invalid argv guard
        pass
    if candidate in renderings:
        return True
    for posix in (True, False):
        try:
            if shlex.split(candidate, posix=posix) == argv:
                return True
        except ValueError:
            continue
    return False


def is_success_wrapped(
    command: str,
    success_marker: str,
    *,
    working_directory: str | Path | None = None,
) -> bool:
    if not success_marker:
        return True
    candidate = _command_after_same_directory_prefix(command, working_directory)
    if candidate is None:
        return False
    suffix = f") && printf '\\n{success_marker}\\n'"
    return candidate.startswith("(") and candidate.endswith(suffix)


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
