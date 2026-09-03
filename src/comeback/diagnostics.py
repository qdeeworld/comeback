from __future__ import annotations

import json
import hashlib
import os
import shlex
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

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib

from .identity import repository_identity
from .memory import InterventionMemory
from .signing import intervention_message


class DiagnosticFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        next_action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action
        self.details = details or {}

    def as_dict(self, agent: str) -> dict[str, Any]:
        return {
            "agent": agent,
            "code": self.code,
            "message": str(self),
            "next": self.next_action,
            **self.details,
        }


def _comeback_handlers(path: Path, *, agent: str) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        configured = document["hooks"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DiagnosticFailure(
            "HOOK_CONFIG_MISSING",
            f"cannot read Comeback hooks from {path}",
            next_action="Run `comeback init`, review the written hook file, then rerun `comeback doctor`.",
        ) from exc
    if not isinstance(configured, dict):
        raise DiagnosticFailure(
            "HOOK_CONFIG_MISSING",
            f"Comeback hook configuration has an invalid shape in {path}",
            next_action="Run `comeback init`, review the written hook file, then rerun `comeback doctor`.",
        )
    found: dict[str, dict[str, Any]] = {}
    for event_name in ("UserPromptSubmit", "PreToolUse", "Stop"):
        groups = configured.get(event_name)
        if not isinstance(groups, list):
            groups = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            if event_name == "PreToolUse" and group.get("matcher") != "Bash":
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if not isinstance(handler, dict):
                    continue
                if "comeback-hook" not in str(handler.get("command", "")):
                    continue
                if agent == "codex" and os.name == "nt" and not isinstance(
                    handler.get("commandWindows"), str
                ):
                    continue
                found[event_name] = handler
                break
            if event_name in found:
                break
        if event_name not in found:
            raise DiagnosticFailure(
                "HOOK_CONFIG_MISSING",
                f"Comeback {event_name} hook is missing from {path}",
                next_action="Run `comeback init`, review every written hook, then rerun `comeback doctor`.",
            )
    command_signatures = {
        (
            handler.get("command"),
            handler.get("commandWindows") if agent == "codex" else None,
        )
        for handler in found.values()
    }
    if len(command_signatures) != 1:
        raise DiagnosticFailure(
            "HOOK_CONFIG_INCONSISTENT",
            f"Comeback lifecycle hooks do not share one trusted launcher in {path}",
            next_action="Run `comeback init`, review every updated hook, then rerun the doctor.",
        )
    return found


def _capability_executable(handler: dict[str, Any]) -> str:
    command = handler.get("command")
    if not isinstance(command, str):
        raise DiagnosticFailure(
            "CAPABILITY_COMMAND_MISSING",
            "Comeback hook does not identify its trusted capability executable",
            next_action="Run `comeback init`, review the updated hooks, then rerun the doctor.",
        )
    try:
        words = shlex.split(command, posix=True)
        option = words.index("--cli-executable")
        executable = words[option + 1]
    except (ValueError, IndexError) as exc:
        raise DiagnosticFailure(
            "CAPABILITY_COMMAND_MISSING",
            "Comeback hook does not identify its trusted capability executable",
            next_action="Run `comeback init`, review the updated hooks, then rerun the doctor.",
        ) from exc
    path = Path(executable)
    if (
        not path.is_absolute()
        or not path.is_file()
        or (os.name != "nt" and not os.access(path, os.X_OK))
    ):
        raise DiagnosticFailure(
            "CAPABILITY_EXECUTABLE_MISSING",
            f"trusted Comeback capability executable was not found: {executable}",
            next_action="Reinstall Comeback with uv, rerun `comeback init`, then rerun the doctor.",
        )
    return str(path)


def _git_bash() -> str:
    if os.name != "nt":
        bash = shutil.which("bash")
        if bash:
            return bash
        raise DiagnosticFailure(
            "GIT_BASH_MISSING",
            "Bash is required for Claude Code hooks",
            next_action="Install Bash, then rerun `comeback doctor --agent claude`.",
        )

    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        git_path = Path(git).resolve()
        candidates.extend(
            [
                git_path.parent.parent / "bin" / "bash.exe",
                git_path.parent.parent / "usr" / "bin" / "bash.exe",
            ]
        )
    for variable in ("ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.extend(
                [
                    Path(base) / "Git" / "bin" / "bash.exe",
                    Path(base) / "Programs" / "Git" / "bin" / "bash.exe",
                ]
            )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise DiagnosticFailure(
        "GIT_BASH_MISSING",
        "Git Bash was not found; Claude Code hooks cannot be verified on Windows",
        next_action="Install Git for Windows, then rerun `comeback doctor --agent claude`.",
    )


def _invoke_installed_hook(
    *,
    root: Path,
    handler: dict[str, Any],
    agent: str,
    database: Path,
    session_id: str,
    temporary: Path,
) -> dict[str, Any]:
    """Probe the Claude launcher only; Codex uses a real client process below."""

    event = {
        "session_id": session_id,
        "cwd": str(root),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Comeback installation self-check.",
        "model": "comeback-doctor",
    }
    environment = os.environ.copy()
    environment["COMEBACK_MEMORY_DB"] = str(database)

    command = handler.get("command")
    if not isinstance(command, str) or not command:
        raise DiagnosticFailure(
            "HOOK_COMMAND_MISSING",
            f"{agent} hook has no command",
            next_action="Run `comeback init`, review the hook file, then rerun the doctor.",
        )
    if agent == "claude" and os.name == "nt":
        script = temporary / "comeback-doctor-hook.sh"
        script.write_text(command + "\n", encoding="utf-8")
        argv = [_git_bash(), str(script)]
    else:
        argv = ["/bin/sh", "-lc", command]

    completed = subprocess.run(
        argv,
        cwd=root,
        env=environment,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise DiagnosticFailure(
            "HOOK_LAUNCHER_FAILED",
            f"{agent} hook command exited {completed.returncode}",
            next_action="Reinstall Comeback and rerun the doctor before starting a release.",
        )
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DiagnosticFailure(
            "HOOK_PROTOCOL_FAILED",
            f"{agent} hook command did not return lifecycle JSON",
            next_action="Do not invoke comeback-hook manually; reinstall and rerun the doctor.",
        ) from exc
    if "additionalContext" not in output.get("hookSpecificOutput", {}):
        raise DiagnosticFailure(
            "HOOK_PROTOCOL_FAILED",
            f"{agent} hook returned the wrong lifecycle response",
            next_action="Reinstall Comeback and rerun the doctor before starting a release.",
        )
    return output


def _codex_project_trusted(root: Path) -> bool:
    config_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = config_home / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    normalized_root = os.path.normcase(os.path.normpath(str(root.resolve())))
    for configured_path, settings in projects.items():
        if not isinstance(configured_path, str) or not isinstance(settings, dict):
            continue
        normalized_path = os.path.normcase(os.path.normpath(str(Path(configured_path).resolve())))
        if normalized_path == normalized_root and settings.get("trust_level") == "trusted":
            return True
    return False


def _client_check(agent: str, root: Path) -> dict[str, Any]:
    executable_name = "codex" if agent == "codex" else "claude"
    executable = shutil.which(executable_name)
    if not executable:
        raise DiagnosticFailure(
            "CLIENT_NOT_FOUND",
            f"{executable_name} is not available on PATH",
            next_action=f"Install and authenticate {executable_name}, reopen the terminal, then rerun the doctor.",
        )
    try:
        version = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        raise DiagnosticFailure(
            "CLIENT_VERSION_FAILED",
            f"{executable_name} --version could not complete",
            next_action=f"Repair the {executable_name} installation, then rerun the doctor.",
        ) from exc
    if version.returncode != 0:
        raise DiagnosticFailure(
            "CLIENT_VERSION_FAILED",
            f"{executable_name} --version exited {version.returncode}",
            next_action=f"Repair the {executable_name} installation, then rerun the doctor.",
        )
    result = {
        "executable": executable,
        "version": version.stdout.strip() or version.stderr.strip(),
    }
    if agent == "codex":
        try:
            features = subprocess.run(
                [executable, "features", "list"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.SubprocessError as exc:
            raise DiagnosticFailure(
                "HOOK_FEATURE_CHECK_FAILED",
                "Codex hook feature status could not be read",
                next_action="Update Codex CLI and rerun `comeback doctor`.",
            ) from exc
        hooks_enabled = any(
            fields[0] == "hooks" and fields[-1].lower() == "true"
            for line in features.stdout.splitlines()
            if (fields := line.split())
        )
        if features.returncode != 0 or not hooks_enabled:
            raise DiagnosticFailure(
                "HOOKS_DISABLED",
                "Codex hooks are unavailable or disabled",
                next_action="Update Codex CLI, enable `[features] hooks = true`, and rerun the doctor.",
            )
        result["hooks_feature"] = "enabled"
        try:
            login = subprocess.run(
                [executable, "login", "status"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.SubprocessError as exc:
            raise DiagnosticFailure(
                "CODEX_AUTH_CHECK_FAILED",
                "Codex authentication status could not be read",
                next_action="Run `codex login`, then rerun `comeback doctor`.",
            ) from exc
        if login.returncode != 0 or _looks_like_auth_error(login):
            raise DiagnosticFailure(
                "CODEX_AUTH_REQUIRED",
                "Codex is not authenticated, so the end-to-end activation check cannot run",
                next_action="Run `codex login`, complete authentication, then rerun `comeback doctor`.",
            )
        result["authentication"] = "available"
        result["project_trusted"] = _codex_project_trusted(root)
        if not result["project_trusted"]:
            raise DiagnosticFailure(
                "PROJECT_NOT_TRUSTED",
                "Codex has not trusted this repository, so project-local hooks are not active",
                next_action=(
                    "Open `codex` in this repository, choose to trust the directory, open `/hooks`, "
                    "review and trust the Comeback hooks, exit, then rerun `comeback doctor`."
                ),
                details={"client": result},
            )
    return result


def _looks_like_auth_error(completed: subprocess.CompletedProcess[str]) -> bool:
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    markers = (
        "not logged in",
        "login required",
        "log in to",
        "authentication required",
        "authentication failed",
        "unauthorized",
        "401",
        "missing api key",
        "invalid api key",
    )
    return any(marker in output for marker in markers)


def _run_codex_activation_probe(
    *, root: Path, repo_id: str, executable: str
) -> dict[str, Any]:
    """Prove the installed hook runs in a new Codex process without a trust bypass."""

    canary_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="comeback-codex-canary-") as directory:
        database = Path(directory) / "memory.db"
        environment = os.environ.copy()
        environment["COMEBACK_MEMORY_DB"] = str(database)
        prompt = (
            f"Comeback activation canary {canary_id}. Do not use tools or change files. "
            "Reply exactly COMEBACK_DOCTOR_OK."
        )
        argv = [
            executable,
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "-C",
            str(root),
            prompt,
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DiagnosticFailure(
                "CODEX_CANARY_TIMEOUT",
                "the fresh Codex activation canary timed out",
                next_action="Check Codex authentication and connectivity, then rerun `comeback doctor`.",
                details={
                    "activation": {
                        "fresh_process": True,
                        "ephemeral_session": True,
                        "sandbox": "read-only",
                        "hook_trust_bypass": False,
                        "sibyl_write": False,
                    }
                },
            ) from exc

        runs = InterventionMemory(database, repo_id).list_runs()
        activation = {
            "fresh_process": True,
            "ephemeral_session": True,
            "sandbox": "read-only",
            "hook_trust_bypass": False,
            "codex_exit_code": completed.returncode,
            "sibyl_write": bool(runs),
        }
        if runs:
            activation["session_id"] = runs[0]["session_id"]
            activation["hook_process_id"] = runs[0].get("process_id")

        if _looks_like_auth_error(completed):
            raise DiagnosticFailure(
                "CODEX_AUTH_REQUIRED",
                "Codex is not authenticated, so the end-to-end activation check cannot pass",
                next_action="Run `codex login`, complete authentication, then rerun `comeback doctor`.",
                details={"activation": activation},
            )
        if not runs:
            if completed.returncode != 0:
                raise DiagnosticFailure(
                    "CODEX_CANARY_FAILED",
                    f"the fresh Codex activation canary exited {completed.returncode} before Sibyl recorded a run",
                    next_action="Repair the Codex error, then rerun `comeback doctor` before any release.",
                    details={"activation": activation},
                )
            raise DiagnosticFailure(
                "HOOK_NOT_ACTIVATED",
                "a fresh Codex process completed, but the Comeback hook never wrote a Sibyl run",
                next_action=(
                    "Open `codex` in this repository, run `/hooks`, review and trust the exact "
                    "Comeback hook definition, exit, then rerun `comeback doctor`."
                ),
                details={"activation": activation},
            )
        if len(runs) != 1:
            raise DiagnosticFailure(
                "CANARY_SESSION_AMBIGUOUS",
                f"the isolated activation canary created {len(runs)} Sibyl runs instead of one",
                next_action="Inspect the installed hook configuration for duplicate Comeback hooks.",
                details={"activation": activation},
            )
        if completed.returncode != 0:
            raise DiagnosticFailure(
                "CODEX_CANARY_FAILED",
                f"the Comeback hook activated, but the fresh Codex process exited {completed.returncode}",
                next_action="Repair the Codex client error, then rerun `comeback doctor`.",
                details={"activation": activation},
            )
        return activation


def _run_codex_pretool_probe(
    *, root: Path, repo_id: str, executable: str
) -> dict[str, Any]:
    """Prove a real fresh Codex process dispatches PreToolUse before execution."""

    comeback_directory = root / ".comeback"
    comeback_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="doctor-",
        dir=comeback_directory,
        ignore_cleanup_errors=True,
    ) as directory:
        temporary = Path(directory)
        database = temporary / "memory.db"
        marker = temporary / "release-executed.json"
        script = temporary / "release_candidate.py"
        script.write_text(
            "from pathlib import Path\n"
            "Path(__file__).with_name('release-executed.json').write_text('executed\\n')\n",
            encoding="utf-8",
        )
        relative_script = script.relative_to(root).as_posix()
        memory = InterventionMemory(database, repo_id)
        owner = Account.create()
        source_session = "doctor-source-" + uuid.uuid4().hex
        signed_fields = {
            "lesson_id": "release-release_workflow-codex",
            "repo_id": repo_id,
            "task_class": "release",
            "area": "release_workflow",
            "agent_family": "Codex",
            "agent_scope": "same_agent",
            "severity": "release_blocker",
            "action_schema": 2,
            "checkpoint_spec": {
                "argv": [sys.executable, "-c", "print('doctor checkpoint')"],
                "timeout_seconds": 60,
            },
            "release_spec": {
                "argv": [sys.executable, relative_script],
                "timeout_seconds": 60,
            },
            "state_policy": {"bind_head": False, "require_clean_git": False},
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
            model="comeback-doctor-source",
        )
        signature = Account.sign_message(
            encode_defunct(text=intervention_message(signed_fields)),
            private_key=owner.key,
        ).signature.hex()
        memory.record_intervention(
            {
                "signed_fields": signed_fields,
                "intervention_signature": signature,
                "incident_summary": "Comeback doctor disposable release denial.",
            }
        )

        environment = os.environ.copy()
        environment["COMEBACK_MEMORY_DB"] = str(database)
        environment["PATH"] = (
            str(Path(sys.executable).parent)
            + os.pathsep
            + environment.get("PATH", "")
        )
        command_text = f"python {relative_script}"
        argv = [
            executable,
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(root),
            (
                "Comeback enforcement canary. Attempt exactly this shell command once: "
                f"{command_text}. Do not run a checkpoint, invoke comeback-hook manually, "
                "disable hooks, or use another command. Report whether Comeback blocked it."
            ),
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DiagnosticFailure(
                "CODEX_PRETOOL_TIMEOUT",
                "the fresh Codex PreToolUse canary timed out",
                next_action="Check Codex connectivity, then rerun the doctor before any release.",
            ) from exc

        combined = completed.stdout + "\n" + completed.stderr
        runs = memory.list_runs(limit=10)
        # The probe seeds one real-looking source run so the signed intervention
        # has provenance.  The proof must select the *new* Codex lifecycle run,
        # not count that fixture as a second ambiguous release session.
        release_runs = [
            run
            for run in runs
            if run.get("task_class") == "release"
            and run.get("session_id") != source_session
        ]
        decisions = (
            memory.pretool_decisions(release_runs[0]["session_id"])
            if len(release_runs) == 1
            else []
        )
        denials = [
            event
            for event in decisions
            if event.get("acted", {}).get("decision") == "deny"
        ]
        expected_command_sha256 = hashlib.sha256(command_text.encode()).hexdigest()
        proof = {
            "fresh_process": True,
            "ephemeral_session": True,
            "sandbox": "workspace-write",
            "hook_trust_bypass": False,
            "codex_exit_code": completed.returncode,
            "sibyl_release_run": len(release_runs) == 1,
            "mode": release_runs[0].get("mode") if len(release_runs) == 1 else None,
            "side_effect_absent": not marker.exists(),
            "pretool_events": len(decisions),
            "pretool_denials": len(denials),
            "pretool_event_id": denials[0].get("id") if len(denials) == 1 else None,
            "command_sha256": (
                denials[0].get("evaluated", {}).get("command_sha256")
                if len(denials) == 1
                else None
            ),
            "expected_command_sha256": expected_command_sha256,
            "denial_visible": (
                "remembered intervention requires" in combined
                or "Comeback HUMAN_REQUIRED" in combined
            ),
        }
        if release_runs:
            proof["session_id"] = release_runs[0]["session_id"]
            proof["hook_process_id"] = release_runs[0].get("process_id")
        if _looks_like_auth_error(completed):
            raise DiagnosticFailure(
                "CODEX_AUTH_REQUIRED",
                "Codex authentication failed during the PreToolUse canary",
                next_action="Run `codex login`, then rerun `comeback doctor`.",
                details={"enforcement": proof},
            )
        if marker.exists():
            raise DiagnosticFailure(
                "PRETOOL_NOT_ENFORCED",
                "the disposable release action executed instead of being blocked",
                next_action=(
                    "Do not release. Open `codex`, run `/hooks`, review and trust every "
                    "Comeback handler, exit, then rerun the doctor."
                ),
                details={"enforcement": proof},
            )
        if (
            completed.returncode != 0
            or len(release_runs) != 1
            or release_runs[0].get("mode") != "HUMAN_REQUIRED"
            or len(denials) != 1
            or denials[0].get("evaluated", {}).get("command_sha256")
            != expected_command_sha256
            or denials[0].get("evaluated", {}).get("tool_use_id")
            in {None, "", "unknown"}
            or not proof["denial_visible"]
        ):
            raise DiagnosticFailure(
                "PRETOOL_NOT_PROVEN",
                "the disposable command did not produce a complete real PreToolUse denial proof",
                next_action=(
                    "Do not release. Review `/hooks`, then rerun the doctor and inspect "
                    "the structured enforcement result."
                ),
                details={"enforcement": proof},
            )
        return proof


def diagnose_repository(repo: str | Path, *, agents: tuple[str, ...] = ("codex",)) -> dict[str, Any]:
    root, repo_id = repository_identity(repo)
    checks: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for agent in agents:
        try:
            config_path = (
                root / ".codex" / "hooks.json"
                if agent == "codex"
                else root / ".claude" / "settings.json"
            )
            handlers = _comeback_handlers(config_path, agent=agent)
            handler = handlers["UserPromptSubmit"]
            capability_executable = _capability_executable(handler)
            client = _client_check(agent, root)
            if agent == "codex":
                activation = _run_codex_activation_probe(
                    root=root,
                    repo_id=repo_id,
                    executable=str(client["executable"]),
                )
                enforcement = _run_codex_pretool_probe(
                    root=root,
                    repo_id=repo_id,
                    executable=str(client["executable"]),
                )
                checks[agent] = {
                    "gate": "PASS",
                    "code": "CODEX_HOOKS_ACTIVE",
                    **client,
                    "config": str(config_path),
                    "capability_executable": capability_executable,
                    "activation": activation,
                    "enforcement": enforcement,
                    "agent_activation_proven": True,
                    "pretool_enforcement_proven": True,
                }
            else:
                with tempfile.TemporaryDirectory(prefix="comeback-doctor-") as directory:
                    temporary = Path(directory)
                    database = temporary / "memory.db"
                    session_id = f"doctor-{agent}-{uuid.uuid4()}"
                    output = _invoke_installed_hook(
                        root=root,
                        handler=handler,
                        agent=agent,
                        database=database,
                        session_id=session_id,
                        temporary=temporary,
                    )
                    run = InterventionMemory(database, repo_id).get_run(session_id)
                checks[agent] = {
                    "gate": "PARTIAL",
                    "code": "CLAUDE_LAUNCHER_ONLY",
                    **client,
                    "config": str(config_path),
                    "capability_executable": capability_executable,
                    "launcher_probe": bool(output),
                    "sibyl_write": run["session_id"] == session_id,
                    "agent_activation_proven": False,
                    "verification_scope": "installed launcher only",
                    "next": (
                        "Run the authenticated real-Claude cross-agent and unlock gates; "
                        "the doctor alone does not prove Claude lifecycle dispatch."
                    ),
                }
        except DiagnosticFailure as exc:
            failure = exc.as_dict(agent)
            checks[agent] = {"gate": "FAIL", **failure}
            errors.append(failure)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failure = DiagnosticFailure(
                "DIAGNOSTIC_ERROR",
                str(exc),
                next_action="Repair this error and rerun the doctor before any release.",
            ).as_dict(agent)
            checks[agent] = {"gate": "FAIL", **failure}
            errors.append(failure)
    has_partial = any(check.get("gate") == "PARTIAL" for check in checks.values())
    return {
        "gate": "FAIL" if errors else ("PARTIAL" if has_partial else "PASS"),
        "repo": str(root),
        "checks": checks,
        "errors": errors,
        "next": (
            "Codex activation is proven in a fresh read-only process; start a new working session."
            if not errors and agents == ("codex",)
            else (
                "The requested checks completed; inspect each agent's verification scope."
                if not errors
                else errors[0]["next"]
            )
        ),
    }
