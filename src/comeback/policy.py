from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PurePath
from typing import Any


MODES = ("AUTONOMOUS", "CHECKPOINTED", "HUMAN_REQUIRED")
WORKFLOW_AREAS = ("release_workflow", "migration_workflow")

_MIGRATION_PROMPT = re.compile(
    r"\A\s*(?:please\s+)?(?:"
    r"(?:apply|run|execute)\s+(?:(?:the|this|a)\s+)?(?:database\s+|db\s+)?migration"
    r"|migrate\s+(?:(?:the|this|a)\s+)?(?:database|db|schema)"
    r")(?:\s+now)?[.!]?\s*\Z", re.I
)

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
_POSIX_SHELLS = {"bash", "dash", "sh", "zsh"}
_WINDOWS_COMMAND_SHELLS = {"cmd"}
_POWERSHELLS = {"powershell", "pwsh"}
_SHELLS = _POSIX_SHELLS | _WINDOWS_COMMAND_SHELLS | _POWERSHELLS
_COMMAND_SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "(", ")", "{", "}"}
_CONTROL_PREFIXES = {"if", "then", "else", "elif", "while", "until", "do"}


class CommandParseError(ValueError):
    """A shell command cannot safely be classified; it is not a safe negative."""


def classify_task(prompt: str) -> tuple[str, str]:
    if _MIGRATION_PROMPT.search(prompt):
        return "release", "migration_workflow"
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
    words = _shell_words(command_from_event(event))
    return _contains_release_action(words)


def comeback_capability_action(event: dict[str, Any]) -> str | None:
    """Return a Comeback capability subcommand found in a shell tool call.

    Exact invocation matching remains the authority. This parser exists so a
    relative launcher, alternate database, extra global option, or Python
    module spelling is recognized and denied rather than silently bypassing
    the hook's objective decision record.
    """

    if event.get("tool_name") != "Bash":
        return None
    words = _shell_words(command_from_event(event))
    actions = {
        action
        for segment in _command_segments(words)
        if (action := _segment_comeback_action(segment)) is not None
    }
    return next(iter(actions)) if len(actions) == 1 else ("multiple" if actions else None)


def _shell_words(command: str, *, preserve_backslashes: bool = False) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n(){}")
    if preserve_backslashes:
        # Used only by the refusal detector's additional Windows spelling pass.
        # The default POSIX tokenizer and exact authorization stay unchanged.
        lexer.escape = ""
    # Keep unquoted newlines as command separators while shlex still preserves
    # a newline inside quotes as part of the quoted argument.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # Do not use shlex's comment stripping: it treats a mid-word # as a
    # comment, unlike Bash, and can hide a later command. Unbalanced quoting
    # (including in a shell comment) must reach the hook as an explicit refusal.
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise CommandParseError("shell quoting is ambiguous or incomplete") from exc


def _clean_word(word: str) -> str:
    cleaned = word.strip("(){};").replace("\\", "/")
    return PurePath(cleaned).name.lower()


def _executable_name(word: str) -> str:
    name = _clean_word(word)
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _command_segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for word in words:
        if word in _COMMAND_SEPARATORS or (
            word and all(character in ";&|\n(){}" for character in word)
        ):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(word)
    return [segment for segment in segments if segment]


def _contains_release_action(words: list[str]) -> bool:
    return any(_segment_is_release(segment) for segment in _command_segments(words))


def _command_index(words: list[str]) -> int:
    """Skip simple literal assignments, control words and leading redirects."""
    index = 0
    while index < len(words):
        word = words[index]
        if re.match(r"[A-Za-z_][A-Za-z_0-9]*=", word) or word.lower() in _CONTROL_PREFIXES:
            index += 1
            continue
        redirect = re.fullmatch(r"\d*(?:<<<|<<-?|<>|>>?|<)(.*)", word)
        if redirect:
            if redirect.group(1):
                index += 1
            elif index + 1 < len(words):
                index += 2
            else:
                raise CommandParseError("redirection has no literal destination")
            continue
        break
    return index


def _wrapper_tail(executable: str, words: list[str]) -> list[str] | None:
    """Unwrap explicit common options for detection, never for authorization."""
    value_options = {
        "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-D", "--chdir", "-R", "--chroot", "-T", "--command-timeout", "-r", "--role", "-t", "--type"},
        "env": {"-u", "--unset", "-C", "--chdir"},
        "exec": {"-a"},
        "timeout": {"-s", "--signal", "-k", "--kill-after"},
        "command": set(),
        "builtin": set(),
        "nohup": set(),
    }
    flag_options = {
        "sudo": {"-n", "--non-interactive", "-E", "--preserve-env", "-H", "--set-home", "-S", "--stdin", "-b", "--background"},
        "env": {"-i", "--ignore-environment", "-0", "--null"},
        "exec": {"-c", "-l"},
        "timeout": {"--preserve-status", "--foreground", "--verbose"},
        "command": {"-p"},
        "builtin": set(),
        "nohup": set(),
    }
    if executable not in value_options:
        return None
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-") or word == "-":
            break
        if executable == "command" and word in {"-v", "-V"}:
            return []  # Command lookup, not execution.
        if word in value_options[executable]:
            if index + 1 >= len(words):
                raise CommandParseError(f"{executable} option has no value")
            index += 2
        elif word in flag_options[executable] or (
            word.startswith("--") and "=" in word
            and word.split("=", 1)[0] in value_options[executable]
        ):
            index += 1
        elif any(word.startswith(option) and len(word) > len(option)
                 for option in value_options[executable] if len(option) == 2):
            index += 1
        else:
            raise CommandParseError(f"unsupported {executable} wrapper option")
    if executable == "env":
        while index < len(words) and re.match(r"[A-Za-z_][A-Za-z_0-9]*=", words[index]):
            index += 1
    if executable == "timeout":
        if index >= len(words) or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[index]):
            raise CommandParseError("timeout requires a literal duration")
        index += 1
    return words[index:]


def _comeback_subcommand(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index].lower()
        if argument in {"--db", "--repo"}:
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if argument.startswith(("--db=", "--repo=")):
            index += 1
            continue
        return argument if argument in {"checkpoint", "release"} else None
    return None


def _shell_payload(
    executable: str,
    words: list[str],
    normalized: list[str],
    index: int,
) -> str | None:
    if executable in _POSIX_SHELLS:
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
            return None
        # For POSIX shells only the next argv item is the -c program; later
        # items become $0/$1 and must not be reinterpreted as commands.
        return words[command_option + 1]
    if executable in _WINDOWS_COMMAND_SHELLS:
        command_option = next(
            (
                option_index
                for option_index in range(index + 1, len(words))
                if words[option_index].lower() in {"/c", "/k"}
            ),
            None,
        )
    else:
        command_option = next(
            (
                option_index
                for option_index in range(index + 1, len(words))
                if words[option_index].lower()
                in {"-c", "-command", "-commandwithargs"}
            ),
            None,
        )
    if command_option is None or command_option + 1 >= len(words):
        return None
    # cmd.exe and PowerShell consume the remaining command line.
    return " ".join(words[command_option + 1 :])


def _segment_comeback_action(words: list[str]) -> str | None:
    if not words:
        return None
    normalized = [_clean_word(word) for word in words]
    index = _command_index(words)
    if index >= len(normalized):
        return None
    executable = _executable_name(words[index])
    wrapper = _wrapper_tail(executable, words[index + 1 :])
    if wrapper is not None:
        return _segment_comeback_action(wrapper)
    if executable in _SHELLS:
        payload = _shell_payload(executable, words, normalized, index)
        if payload is None:
            return None
        nested = _shell_words(payload)
        nested_actions = {
            action
            for segment in _command_segments(nested)
            if (action := _segment_comeback_action(segment)) is not None
        }
        return next(iter(nested_actions)) if len(nested_actions) == 1 else (
            "multiple" if nested_actions else None
        )
    if executable in {"eval", "iex", "invoke-expression"} and index + 1 < len(words):
        nested = _shell_words(" ".join(words[index + 1 :]))
        nested_actions = {
            action
            for segment in _command_segments(nested)
            if (action := _segment_comeback_action(segment)) is not None
        }
        return next(iter(nested_actions)) if len(nested_actions) == 1 else (
            "multiple" if nested_actions else None
        )
    raw_tail = words[index + 1 :]
    if executable == "comeback":
        return _comeback_subcommand(raw_tail)
    if _interpreter_family(executable) == "python":
        entrypoint = _interpreter_entrypoint(executable, raw_tail)
        if entrypoint is not None and entrypoint[:2] == ("module", "comeback.cli"):
            return _comeback_subcommand(entrypoint[2])
    return None


def _segment_is_release(words: list[str]) -> bool:
    if not words:
        return False
    normalized = [_clean_word(word) for word in words]
    index = _command_index(words)
    if index >= len(normalized):
        return False

    executable = _executable_name(words[index])
    wrapper = _wrapper_tail(executable, words[index + 1 :])
    if wrapper is not None:
        return _segment_is_release(wrapper)
    if executable in _SHELLS:
        payload = _shell_payload(executable, words, normalized, index)
        if payload is None:
            return False
        return _contains_release_action(_shell_words(payload))
    if executable in {"eval", "iex", "invoke-expression"} and index + 1 < len(words):
        return _contains_release_action(_shell_words(" ".join(words[index + 1 :])))

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
    if _segment_comeback_action(words) == "release":
        return True
    if _interpreter_family(executable) == "python":
        entrypoint = _interpreter_entrypoint(executable, words[index + 1 :])
        return bool(entrypoint is not None and entrypoint[0] == "script"
                    and _clean_word(entrypoint[1]) in {"release_candidate.py", "release-candidate.py"})
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
    # Check shell literalness before Path.resolve() can erase a component
    # containing an evaluated expression followed by /.. Use the exact,
    # unprefixed capability for paths outside this conservative grammar. Reject
    # control operators even inside quotes: single quotes are not protective in
    # cmd.exe, and this matcher is shared by multiple shell dialects.
    if not raw_target or any(
        ord(character) < 32 or character in "$`%!^;&|<>()"
        for character in raw_target
    ):
        return None
    if raw_target[0] in {"'", '"'}:
        if len(raw_target) < 2 or raw_target[-1] != raw_target[0]:
            return None
        raw_target = raw_target[1:-1]
        if any(character in "\"'" for character in raw_target):
            return None
    elif any(
        character.isspace() or character in "\"';&|<>(){}[]*?~#@,"
        for character in raw_target
    ):
        return None
    if not raw_target or raw_target.startswith("-"):
        return None
    target = Path(raw_target)
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


def detects_configured_argv(
    command: str,
    argv: list[str],
    *,
    working_directory: str | Path | None = None,
) -> bool:
    """Detect a configured raw action, without authorizing any equivalent form.

    Direct interpreter/script variants and common explicit wrappers are treated
    conservatively as protected. Only invocation_matches can allow a capability;
    this detector never reconstructs or changes the signed executable vector.
    Arbitrary script contents and other processes remain outside this boundary.
    """

    candidate = _command_after_same_directory_prefix(command, working_directory)
    if candidate is None:
        # A prefix outside the exact authorization grammar must not hide the
        # later configured action from this refusal-only detector.
        candidate = command
    candidate = candidate.strip()
    if candidate.startswith("& "):
        candidate = candidate[2:].strip()
    # Parse first even when another shell's rendering might happen to compare
    # equal: a lexical failure is an explicit uncertainty, never a safe miss.
    words = _shell_words(candidate)
    renderings = {shlex.join(argv)}
    try:
        import subprocess

        renderings.add(subprocess.list2cmdline(argv))
    except (ImportError, ValueError):  # pragma: no cover - stdlib/invalid argv guard
        pass
    if candidate in renderings:
        return True
    if _segments_invoke_configured(words, argv, working_directory):
        return True
    if os.name == "nt" and "\\" in candidate:
        if _segments_invoke_configured(
            _shell_words(candidate, preserve_backslashes=True), argv,
            working_directory, preserve_backslashes=True,
        ):
            return True
    for posix in (True, False):
        try:
            if shlex.split(candidate, posix=posix) == argv:
                return True
        except ValueError:
            continue
    return False


def invokes_configured_argv(
    command: str,
    argv: list[str],
    *,
    working_directory: str | Path | None = None,
) -> bool:
    """Match a literal argv vector, including for the read-only doctor probe.

    Keep this strict: detectors may broaden refusals, but callers establishing
    an allowed diagnostic invocation must not accept prefixes or extra actions.
    """
    candidate = _command_after_same_directory_prefix(command, working_directory)
    if candidate is None:
        return False
    candidate = candidate.strip()
    if candidate.startswith("& "):
        candidate = candidate[2:].strip()
    import subprocess

    if candidate in {shlex.join(argv), subprocess.list2cmdline(argv)}:
        return True
    for posix in (True, False):
        try:
            if shlex.split(candidate, posix=posix) == argv:
                return True
        except ValueError:
            continue
    return False


def _interpreter_family(executable: str) -> str | None:
    if executable == "py" or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return "python"
    if executable in {"node", "nodejs"}:
        return "node"
    return None


def _interpreter_entrypoint(executable: str, arguments: list[str]) -> tuple[str, str, list[str]] | None:
    """Find a direct Python/Node entry point for refusal-only classification.

    Interpreter options precede the script/module; they are not themselves
    script names. Inline programs and stdin are deliberately not interpreted.
    Unknown option grammar is uncertainty, never evidence of a safe command.
    """
    family = _interpreter_family(executable)
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            return ("script", arguments[index], arguments[index + 1 :]) if index < len(arguments) else None
        if argument == "-":
            return None
        if not argument.startswith("-"):
            return "script", argument, arguments[index:]
        if argument in {"--help", "--version", "-h", "-V", "-?"}:
            return None
        if family == "python":
            if executable == "py" and re.fullmatch(r"-(?:\d+(?:\.\d+)*(?:-\d+)?|V:.+)", argument):
                continue
            if argument in {"--help-env", "--help-xoptions", "--help-all"}:
                return None
            if argument == "--check-hash-based-pycs":
                if index >= len(arguments):
                    raise CommandParseError("Python option has no value")
                index += 1
                continue
            if argument.startswith("--check-hash-based-pycs="):
                continue
            # CPython accepts clustered flags and attached -W/-X/-m values.
            options = argument[1:]
            position = 0
            while position < len(options):
                option = options[position]
                position += 1
                if option in "bBdEiIOPqRsSuvx":
                    continue
                if option in "hV?":
                    return None
                if option == "c":
                    return None
                if option not in {"W", "X", "m"}:
                    raise CommandParseError("unsupported Python interpreter option")
                value = options[position:]
                if not value:
                    if index >= len(arguments):
                        raise CommandParseError("Python option has no value")
                    value = arguments[index]
                    index += 1
                if option == "m":
                    return "module", value, arguments[index:]
                break  # Everything after -W/-X is its value, not more flags.
        elif family == "node":
            if argument in {"-e", "--eval", "-p", "--print", "-v"} or argument.startswith(("--eval=", "--print=")):
                return None
            value_options = {"-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file", "--env-file-if-exists"}
            if argument in value_options:
                if index >= len(arguments):
                    raise CommandParseError("Node option has no value")
                index += 1
            elif "=" in argument and argument.split("=", 1)[0] in value_options:
                continue
            elif argument not in {"--no-warnings", "--trace-warnings", "--use-strict", "--enable-source-maps", "--disable-proto=delete", "--disable-proto=throw", "--test"}:
                raise CommandParseError("unsupported Node interpreter option")
    return None


def _segment_changes_directory(words: list[str]) -> bool:
    index = _command_index(words)
    if index >= len(words):
        return False
    executable = _executable_name(words[index])
    if executable in {"cd", "chdir", "pushd", "popd", "set-location", "sl", "eval", "iex", "invoke-expression"}:
        return True
    wrapper = _wrapper_tail(executable, words[index + 1:])
    return wrapper is not None and _segment_changes_directory(wrapper)


def _segments_invoke_configured(
    words: list[str], argv: list[str], working_directory: str | Path | None,
    *, directory_uncertain: bool = False, preserve_backslashes: bool = False,
) -> bool:
    for segment in _command_segments(words):
        index = _command_index(segment)
        if index >= len(segment):
            continue
        executable = _executable_name(segment[index])
        if executable in {"cd", "chdir", "pushd", "popd", "set-location", "sl"}:
            # Do not simulate shell directory stacks, conditional success or
            # variable expansion. A later same-named relative script is unsafe
            # to classify as unrelated when its resolution base has changed.
            directory_uncertain = True
            continue
        if _segment_invokes_configured(
            segment, argv, working_directory,
            directory_uncertain=directory_uncertain,
            preserve_backslashes=preserve_backslashes,
        ):
            return True
        if _segment_changes_directory(segment):
            directory_uncertain = True
    return False


def _configured_script_path_matches(
    actual: str, expected: str, working_directory: str | Path | None,
    *, directory_uncertain: bool,
) -> bool:
    root = Path(working_directory) if working_directory is not None else Path.cwd()
    try:
        actual_path, expected_path = Path(actual), Path(expected)
        actual_name, expected_name = actual_path.name, expected_path.name
        if os.name == "nt":
            actual_name, expected_name = actual_name.casefold(), expected_name.casefold()
        if directory_uncertain and not actual_path.is_absolute() and actual_name == expected_name:
            return True
        return (root / actual_path).resolve() == (root / expected_path).resolve()
    except (OSError, ValueError) as exc:
        raise CommandParseError("configured script identity cannot be resolved") from exc


def _segment_invokes_configured(
    words: list[str], argv: list[str], working_directory: str | Path | None,
    *, directory_uncertain: bool = False, preserve_backslashes: bool = False,
) -> bool:
    if not words or not argv:
        return False
    index = _command_index(words)
    if index >= len(words):
        return False
    words = words[index:]
    executable = _executable_name(words[0])
    wrapper = _wrapper_tail(executable, words[1:])
    if wrapper is not None:
        directory_options = {
            "env": {"-C", "--chdir"},
            "sudo": {"-D", "--chdir", "-R", "--chroot"},
        }.get(executable, set())
        changes_directory = any(
            word == option or word.startswith(option + "=")
            or (len(option) == 2 and word.startswith(option) and len(word) > 2)
            for word in words[1:len(words) - len(wrapper)] for option in directory_options
        )
        return _segment_invokes_configured(
            wrapper, argv, working_directory,
            directory_uncertain=directory_uncertain or changes_directory,
            preserve_backslashes=preserve_backslashes,
        )
    if executable in _SHELLS:
        payload = _shell_payload(executable, words, [_clean_word(word) for word in words], 0)
        return payload is not None and _segments_invoke_configured(
            _shell_words(payload, preserve_backslashes=preserve_backslashes),
            argv, working_directory, directory_uncertain=directory_uncertain,
            preserve_backslashes=preserve_backslashes,
        )
    if executable in {"eval", "iex", "invoke-expression"}:
        return _segments_invoke_configured(
            _shell_words(" ".join(words[1:]), preserve_backslashes=preserve_backslashes),
            argv, working_directory, directory_uncertain=directory_uncertain,
            preserve_backslashes=preserve_backslashes,
        )
    expected = _executable_name(argv[0])
    family = _interpreter_family(executable)
    if executable != expected and not (family and family == _interpreter_family(expected)):
        # A known interpreter script may also be executable through its shebang.
        # Recognize the same explicit path for refusal, without reading scripts,
        # resolving arbitrary PATH aliases, or widening capability authorization.
        explicit_path = (
            "/" in words[0] or "\\" in words[0]
            or (os.name == "nt" and bool(Path(words[0]).drive))
        )
        if _interpreter_family(expected) and explicit_path:
            try:
                entry = _interpreter_entrypoint(expected, argv[1:])
            except CommandParseError:
                entry = None  # An unknown signed option does not identify a script.
            if entry is not None and entry[0] == "script":
                return _configured_script_path_matches(
                    words[0], entry[1], working_directory,
                    directory_uncertain=directory_uncertain,
                )
        return False
    # Added arguments cannot turn a known entry point into an unprotected one.
    # This broadens refusal only, not the exact one-shot authorization grammar.
    if len(words) >= len(argv) and words[1:len(argv)] == argv[1:]:
        return True
    if family:
        expected_entry = _interpreter_entrypoint(expected, argv[1:])
        actual_entry = _interpreter_entrypoint(executable, words[1:])
        if expected_entry is None or actual_entry is None:
            return False
        if expected_entry[0] != actual_entry[0]:
            return False
        if expected_entry[0] == "module":
            return expected_entry[1] == actual_entry[1]
        return _configured_script_path_matches(
            actual_entry[1], expected_entry[1], working_directory,
            directory_uncertain=directory_uncertain,
        )
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
    # Outcomes retire repeated human approval, never the remembered verifier.
    # AUTONOMOUS is reserved for a run with no matching lesson.
    if successes >= failures:
        return "CHECKPOINTED"
    return "HUMAN_REQUIRED"


def requirements_for_mode(mode: str) -> list[str]:
    if mode == "HUMAN_REQUIRED":
        return ["release_check_passed", "human_approval"]
    if mode == "CHECKPOINTED":
        return ["release_check_passed"]
    return []
