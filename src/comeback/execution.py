from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .memory import (
    InterventionMemory,
    MemoryIntegrityError,
    release_destination,
    utc_now,
)
from .signing import action_spec_digest, checkpoint_receipt_digest


def _result(completed: subprocess.CompletedProcess[str], run: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": run["session_id"],
        "mode": run["mode"],
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
    }


def _git(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MemoryIntegrityError(
            f"git {' '.join(arguments)} could not complete: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise MemoryIntegrityError(
            f"git {' '.join(arguments)} failed: {completed.stderr.decode(errors='replace').strip()}"
        )
    return completed.stdout


def _git_branch(root: Path) -> str:
    completed = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "(detached HEAD)"


def _file_identity(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise MemoryIntegrityError(f"action input is not a regular file: {path}")
    return {
        "path": os.path.normcase(str(resolved)),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def _action_context(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    argv = spec["argv"]
    executable_text = argv[0]
    executable = Path(executable_text).expanduser()
    if not executable.is_absolute():
        if (
            "/" in executable_text
            or "\\" in executable_text
            or executable_text.startswith(".")
        ):
            executable = root / executable
        else:
            discovered = shutil.which(executable_text)
            if not discovered:
                raise MemoryIntegrityError(
                    f"signed action executable is unavailable: {argv[0]}"
                )
            executable = Path(discovered)

    argument_files: list[dict[str, str]] = []
    for argument in argv[1:]:
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            argument_files.append(_file_identity(candidate))

    sensitive_environment = {
        key: value
        for key, value in os.environ.items()
        if key in {
            "PATH",
            "PATHEXT",
            "PYTHONHOME",
            "PYTHONPATH",
            "NODE_OPTIONS",
            "NODE_PATH",
            "RUBYOPT",
            "PERL5OPT",
            "BASH_ENV",
            "ENV",
            "COMSPEC",
            "SHELL",
            "VIRTUAL_ENV",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "SSH_AUTH_SOCK",
        }
        or key.startswith(("GIT_", "AWS_", "CLOUDFLARE_", "VERCEL_"))
        or key.endswith(("_TOKEN", "_SECRET", "_API_KEY"))
    }
    return {
        "executable": _file_identity(executable),
        "argument_files": sorted(argument_files, key=lambda item: item["path"]),
        "environment_sha256": hashlib.sha256(
            json.dumps(
                sensitive_environment,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }


def repository_fingerprint(
    root: Path,
    policy: dict[str, bool],
    *,
    checkpoint_spec: dict[str, Any],
    release_spec: dict[str, Any],
) -> str:
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if policy["require_clean_git"] and status:
        raise MemoryIntegrityError("repository must be clean at checkpoint and release")
    tracked_diff = _git(root, "diff", "--binary", "HEAD", "--", ".")
    staged_diff = _git(root, "diff", "--binary", "--cached", "HEAD", "--", ".")
    untracked_names = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    effective_git_config = _git(root, "config", "--null", "--list", "--show-origin")
    hooks_path = Path(_git(root, "rev-parse", "--git-path", "hooks").decode().strip())
    if not hooks_path.is_absolute():
        hooks_path = root / hooks_path
    git_hooks: list[dict[str, str]] = []
    if hooks_path.is_dir():
        for hook_path in sorted(hooks_path.rglob("*")):
            if hook_path.is_file():
                git_hooks.append(_file_identity(hook_path))
    untracked: list[dict[str, str]] = []
    for encoded_name in filter(None, untracked_names.split(b"\0")):
        name = encoded_name.decode("utf-8", errors="surrogateescape")
        path = root / name
        if path.is_symlink():
            digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = "unsupported"
        untracked.append({"path": name, "sha256": digest})
    state = {
        "head": head if policy["bind_head"] else None,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
        "staged_diff_sha256": hashlib.sha256(staged_diff).hexdigest(),
        "untracked": untracked,
        "effective_git_config_sha256": hashlib.sha256(effective_git_config).hexdigest(),
        "git_hooks": git_hooks,
        "checkpoint_context": _action_context(root, checkpoint_spec),
        "release_context": _action_context(root, release_spec),
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def release_scope(repo_id: str) -> str:
    return json.dumps(
        {"repo_id": repo_id, "protected_capability": "release_workflow"},
        sort_keys=True,
    )


def release_lock_path(root: Path, repo_id: str) -> Path:
    scope = release_scope(repo_id)
    lock_name = hashlib.sha256(scope.encode()).hexdigest() + ".lock"
    return root / ".comeback" / "release-locks" / lock_name


def read_release_lock(root: Path, repo_id: str) -> dict[str, Any] | None:
    path = release_lock_path(root, repo_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryIntegrityError("release lock is unreadable or corrupted") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "scope",
            "session_id",
            "wrapper_process_id",
            "wrapper_process_birth_token",
            "child_process_id",
            "child_process_birth_token",
            "process_group_id",
            "child_started_at",
            "phase",
            "nonce",
        }
        or value.get("scope") != release_scope(repo_id)
        or not isinstance(value.get("session_id"), str)
        or not value["session_id"]
        or not isinstance(value.get("wrapper_process_id"), int)
        or value["wrapper_process_id"] < 1
        or not isinstance(value.get("wrapper_process_birth_token"), str)
        or not value["wrapper_process_birth_token"]
        or value.get("phase")
        not in {
            "reserved",
            "barrier_ready",
            "start_authorized",
            "child_started",
            "child_exited",
            "child_stopped",
            "launch_failed",
            "outcome_unknown_persisted",
        }
        or not isinstance(value.get("nonce"), str)
        or not value["nonce"]
    ):
        raise MemoryIntegrityError("release lock is corrupted or belongs to another scope")
    child_id = value.get("child_process_id")
    child_birth_token = value.get("child_process_birth_token")
    process_group_id = value.get("process_group_id")
    child_started_at = value.get("child_started_at")
    if value["phase"] in {"barrier_ready", "start_authorized", "child_started"}:
        if (
            not isinstance(child_id, int)
            or child_id < 1
            or not isinstance(child_birth_token, str)
            or not child_birth_token
            or not isinstance(process_group_id, int)
            or process_group_id < 1
            or not isinstance(child_started_at, str)
            or not child_started_at
        ):
            raise MemoryIntegrityError("executing release lock has no child identity")
    elif child_id is not None and (not isinstance(child_id, int) or child_id < 1):
        raise MemoryIntegrityError("release lock child identity is invalid")
    elif child_id is not None and (
        not isinstance(child_birth_token, str) or not child_birth_token
    ):
        raise MemoryIntegrityError("release lock child creation identity is invalid")
    elif child_id is None and child_birth_token is not None:
        raise MemoryIntegrityError("release lock has a creation identity without a child")
    elif process_group_id is not None and (
        not isinstance(process_group_id, int) or process_group_id < 1
    ):
        raise MemoryIntegrityError("release lock process group identity is invalid")
    return value


def _process_is_alive(process_id: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            # Access denied is treated as alive; invalid/missing PID is not.
            return ctypes.get_last_error() not in {6, 87, 1168}
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_birth_token(process_id: int) -> str | None:
    """Return an OS creation token so a reused PID is not mistaken for ours."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return None
        try:
            created = FILETIME()
            exited = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return f"windows-filetime:{value}"
        finally:
            kernel32.CloseHandle(handle)

    proc_stat = Path(f"/proc/{process_id}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    if raw:
        # The comm field may contain spaces and parentheses.  Fields after its
        # final ')' start at field 3; Linux starttime is field 22.
        closing = raw.rfind(")")
        remainder = raw[closing + 1 :].split() if closing >= 0 else []
        if len(remainder) > 19:
            return f"linux-starttime:{remainder[19]}"

    if sys.platform == "darwin":
        import ctypes

        class PROC_BSDINFO(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            libproc.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            libproc.proc_pidinfo.restype = ctypes.c_int
            information = PROC_BSDINFO()
            size = ctypes.sizeof(information)
            returned = libproc.proc_pidinfo(
                process_id,
                3,  # PROC_PIDTBSDINFO
                0,
                ctypes.byref(information),
                size,
            )
            if returned == size:
                return (
                    "darwin-starttime:"
                    f"{information.pbi_start_tvsec}:{information.pbi_start_tvusec}"
                )
        except OSError:
            return None

    # Portable fallback for less common POSIX systems. macOS and Linux use
    # native APIs above because agent sandboxes commonly deny launching `ps`.
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(process_id)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return f"ps-lstart:{value}" if completed.returncode == 0 and value else None


def _required_process_birth_token(process_id: int) -> str:
    token = _process_birth_token(process_id)
    if token is None:
        raise MemoryIntegrityError(
            "could not bind the release process to an OS creation identity"
        )
    return token


def _process_identity_is_alive(process_id: int, birth_token: str) -> bool:
    if not _process_is_alive(process_id):
        return False
    current = _process_birth_token(process_id)
    # An access/query failure must not make a potentially live release look
    # dead.  A positive mismatch, however, proves that the PID was reused.
    return current is None or current == birth_token


def _posix_group_is_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def release_process_is_alive(lock: dict[str, Any]) -> bool:
    # Once UNKNOWN is durably stored, the old CLI wrapper is no longer doing
    # result-persistence work. The runner/process-group must still be gone.
    wrapper_alive = lock["phase"] != "outcome_unknown_persisted" and (
        _process_identity_is_alive(
            lock["wrapper_process_id"], lock["wrapper_process_birth_token"]
        )
    )
    child_id = lock.get("child_process_id")
    if isinstance(child_id, int):
        if os.name == "nt":
            return wrapper_alive or _process_identity_is_alive(
                child_id, lock["child_process_birth_token"]
            )
        group_id = lock.get("process_group_id")
        if isinstance(group_id, int) and _posix_group_is_alive(group_id):
            return True
    # Before child publication, and after child exit but before the wrapper
    # durably stores the outcome, the wrapper still owns reconciliation.
    return wrapper_alive


def _publish_lock(path: Path, record: dict[str, Any]) -> None:
    """Publish a complete lock atomically without an empty O_EXCL window."""

    temporary = path.with_name(f".{path.name}.{record['nonce']}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # A hard link is an atomic create-if-absent publication on POSIX and
        # NTFS. It never replaces another session's lock.
        os.link(temporary, path)
    except FileExistsError as exc:
        raise MemoryIntegrityError(
            "release scope is already executing or has an unresolved outcome"
        ) from exc
    except OSError as exc:
        raise MemoryIntegrityError(
            "release lock could not be published atomically on this filesystem"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _update_release_lock(
    root: Path,
    repo_id: str,
    expected: dict[str, Any],
    **changes: Any,
) -> dict[str, Any]:
    path = release_lock_path(root, repo_id)
    current = read_release_lock(root, repo_id)
    if current != expected:
        raise MemoryIntegrityError("release lock changed during capability execution")
    updated = {**current, **changes}
    temporary = path.with_name(f".{path.name}.{current['nonce']}.update")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if read_release_lock(root, repo_id) != expected:
            raise MemoryIntegrityError("release lock changed during capability execution")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return read_release_lock(root, repo_id) or updated


def clear_release_lock(
    root: Path,
    repo_id: str,
    session_id: str,
    *,
    expected: dict[str, Any] | None = None,
) -> bool:
    current = read_release_lock(root, repo_id)
    if current is None:
        return False
    if current["session_id"] != session_id:
        raise MemoryIntegrityError(
            "release lock belongs to another supervision session"
        )
    if expected is not None and current != expected:
        raise MemoryIntegrityError("release lock changed during reconciliation")
    release_lock_path(root, repo_id).unlink()
    return True


def reconcile_release(
    memory: InterventionMemory,
    *,
    session_id: str,
    root: Path,
    resolution: str,
    resolved_at: str,
    signature: str,
) -> dict[str, Any]:
    run = memory.get_run(session_id)
    lock = read_release_lock(root, memory.repo_id)
    if lock is not None and lock["session_id"] != session_id:
        raise MemoryIntegrityError(
            "cannot reconcile this run while another release owns the repository lock"
        )
    if run["status"] == "open":
        if lock is None:
            raise MemoryIntegrityError(
                "open supervision run has no orphaned release reservation to reconcile"
            )
        if release_process_is_alive(lock):
            raise MemoryIntegrityError(
                "reserved release cannot be reconciled while its recorded process is alive"
            )
        if resolution != "not_released":
            raise MemoryIntegrityError(
                "a release that never started can only be reconciled as not_released"
            )
    if run["status"] in {"executing", "unknown", "completed", "failed"} and lock is None:
        raise MemoryIntegrityError(
            "release outcome cannot be reconciled without its matching retained lock"
        )
    if lock is not None and release_process_is_alive(lock):
        raise MemoryIntegrityError(
            "executing release cannot be reconciled until its recorded process is stopped"
        )
    if (
        lock is not None
        and lock["phase"] == "barrier_ready"
        and resolution != "not_released"
    ):
        raise MemoryIntegrityError(
            "the durable start barrier was never authorized; this release can only be reconciled as not_released"
        )
    if (
        run["status"] == "executing"
        and lock is not None
        and lock["phase"] == "reserved"
    ):
        raise MemoryIntegrityError(
            "release child identity was never durably published; reconciliation is fail-closed"
        )
    result = memory.reconcile_release(
        session_id,
        resolution=resolution,
        resolved_at=resolved_at,
        signature=signature,
    )
    result["release_lock_cleared"] = clear_release_lock(
        root,
        memory.repo_id,
        session_id,
        expected=lock,
    )
    return result


def _release_lock(
    root: Path, repo_id: str, session_id: str
) -> tuple[dict[str, Any], Path]:
    scope = release_scope(repo_id)
    lock_path = release_lock_path(root, repo_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "scope": scope,
        "session_id": session_id,
        "wrapper_process_id": os.getpid(),
        "wrapper_process_birth_token": _required_process_birth_token(os.getpid()),
        "child_process_id": None,
        "child_process_birth_token": None,
        "process_group_id": None,
        "child_started_at": None,
        "phase": "reserved",
        "nonce": uuid.uuid4().hex,
    }
    _publish_lock(lock_path, record)
    return record, lock_path


def _pinned_release_command(
    run: dict[str, Any], *, repository_head: str | None = None
) -> list[str]:
    command = list(run["release_spec"]["argv"])
    executable = Path(command[0]).name.lower()
    if executable in {"git", "git.exe"} and command[1] == "push":
        receipt = run.get("checkpoint_receipt")
        approved_head = (
            receipt.get("repository_head")
            if isinstance(receipt, dict)
            else repository_head
        )
        if not isinstance(approved_head, str) or not approved_head:
            raise MemoryIntegrityError("Git release has no immutable approved commit")
        _, destination = command[3].split(":", 1)
        command[3] = f"{approved_head}:{destination}"
    return command


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        stopped = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        if stopped.returncode != 0 and process.poll() is None:
            raise MemoryIntegrityError(
                "Windows could not stop the release process tree; reconciliation remains blocked"
            )
    else:
        def group_alive() -> bool:
            # Reap the group leader when it has exited so a zombie is not
            # mistaken for a still-running descendant.
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 0.5
        while group_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if group_alive():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while group_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if group_alive():
            raise MemoryIntegrityError(
                "release process group could not be stopped; reconciliation remains blocked"
            )
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise MemoryIntegrityError(
            "release process tree could not be stopped; reconciliation remains blocked"
        ) from exc


def _barrier_paths(lock_path: Path, nonce: str) -> tuple[Path, Path]:
    return (
        lock_path.with_name(f".{lock_path.name}.{nonce}.ready"),
        lock_path.with_name(f".{lock_path.name}.{nonce}.start"),
    )


def _wait_for_runner_ready(
    process: subprocess.Popen[str], ready_path: Path, *, timeout: float = 10
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_path.is_file():
            try:
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MemoryIntegrityError("release runner readiness record is invalid") from exc
            if ready != {"process_id": process.pid}:
                raise MemoryIntegrityError("release runner readiness identity is invalid")
            return
        if process.poll() is not None:
            raise MemoryIntegrityError(
                f"release runner exited {process.returncode} before its start barrier"
            )
        time.sleep(0.02)
    raise MemoryIntegrityError("release runner did not reach its start barrier")


def _open_start_barrier(path: Path, nonce: str) -> None:
    temporary = path.with_name(f".{path.name}.{nonce}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (nonce + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # Publish only complete content. The runner can never observe the
        # empty/partial window produced by writing the final path in place.
        os.link(temporary, path)
    except FileExistsError as exc:
        raise MemoryIntegrityError("capability start barrier already exists") from exc
    except OSError as exc:
        raise MemoryIntegrityError(
            "capability start barrier could not be published atomically"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _run_contained_command(
    root: Path,
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run exact argv in the same cross-platform process-tree boundary as release."""

    control = root / ".comeback" / "capability-runners"
    control.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    ready = control / f"{token}.ready"
    start = control / f"{token}.start"
    runner_command = [
        sys.executable,
        "-I",
        "-m",
        "comeback.runner",
        "--ready",
        str(ready),
        "--start",
        str(start),
        "--token",
        token,
        "--wait-seconds",
        "120",
        "--",
        *command,
    ]
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            runner_command,
            cwd=root,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_options,
        )
        _wait_for_runner_ready(process, ready)
        _open_start_barrier(start, token)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_process_tree(process)
            raise MemoryIntegrityError(
                "capability timed out and its process tree was stopped"
            ) from exc
        if os.name != "nt" and _posix_group_is_alive(process.pid):
            _stop_process_tree(process)
            raise MemoryIntegrityError(
                "capability left a background process; no evidence was recorded"
            )
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )
    except BaseException as exc:
        if process is not None:
            _stop_process_tree(process)
        if isinstance(exc, (MemoryIntegrityError, KeyboardInterrupt, SystemExit)):
            raise
        if not isinstance(exc, OSError):
            raise
        raise MemoryIntegrityError(
            f"capability could not start: {type(exc).__name__}"
        ) from exc
    finally:
        ready.unlink(missing_ok=True)
        start.unlink(missing_ok=True)


def execute_checkpoint(
    memory: InterventionMemory,
    *,
    session_id: str,
    root: Path,
    timeout: int = 600,
) -> tuple[dict[str, Any], int]:
    run = memory.get_run(session_id)
    if run["status"] != "open":
        raise MemoryIntegrityError("supervision run is not open")
    run = memory.get_verified_run(session_id)
    spec = run.get("checkpoint_spec")
    if not spec or "release_check_passed" not in run["required_evidence"]:
        raise MemoryIntegrityError("this supervision run has no required checkpoint")
    command = spec["argv"]
    state_before = repository_fingerprint(
        root,
        run["state_policy"],
        checkpoint_spec=spec,
        release_spec=run["release_spec"],
    )
    started_at = utc_now()
    completed = _run_contained_command(
        root,
        command,
        timeout=min(timeout, spec["timeout_seconds"]),
    )
    if completed.returncode == 0:
        state_after = repository_fingerprint(
            root,
            run["state_policy"],
            checkpoint_spec=spec,
            release_spec=run["release_spec"],
        )
        if state_after != state_before:
            raise MemoryIntegrityError(
                "repository or release context changed while the checkpoint ran"
            )
        receipt = {
            "repo_id": memory.repo_id,
            "session_id": session_id,
            "checkpoint_spec_sha256": action_spec_digest(spec),
            "release_spec_sha256": action_spec_digest(run["release_spec"]),
            "state_fingerprint": state_after,
            "repository_head": _git(root, "rev-parse", "HEAD").decode().strip(),
            "repository_branch": _git_branch(root),
            "release_destination": release_destination(run["release_spec"]),
            "started_at": started_at,
            "completed_at": utc_now(),
            "exit_code": 0,
        }
        receipt["digest"] = checkpoint_receipt_digest(receipt)
        run = memory.record_checkpoint_receipt(session_id, receipt)
    result = _result(completed, run)
    result["decision"] = "checkpoint_recorded" if completed.returncode == 0 else "checkpoint_failed"
    result["remaining"] = memory.missing_requirements(run)
    return result, completed.returncode


def execute_release(
    memory: InterventionMemory,
    *,
    session_id: str,
    root: Path,
    timeout: int = 600,
) -> tuple[dict[str, Any], int]:
    run = memory.get_run(session_id)
    if run["status"] != "open":
        raise MemoryIntegrityError("supervision run is not open")
    lock_record, lock_path = _release_lock(root, memory.repo_id, session_id)
    retain_lock = False
    release_begun = False
    process: subprocess.Popen[str] | None = None
    ready_path, start_path = _barrier_paths(lock_path, lock_record["nonce"])
    command: list[str] = []
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        # Re-read every binding only after serializing this repository/lesson
        # scope. A concurrent successful release may have changed autonomy.
        run = memory.get_run(session_id)
        if run["status"] != "open":
            raise MemoryIntegrityError("supervision run is not open")
        run = memory.get_verified_run(session_id)
        if run["task_class"] != "release":
            raise MemoryIntegrityError("supervision run is not a release task")
        missing = memory.missing_requirements(run)
        if missing:
            raise MemoryIntegrityError(
                "release remains blocked; required evidence: " + ", ".join(missing)
            )
        spec = run.get("release_spec")
        if not spec:
            raise MemoryIntegrityError("supervision run has no signed release capability")
        head_before = _git(root, "rev-parse", "HEAD").decode().strip()
        state_fingerprint = repository_fingerprint(
            root,
            run["state_policy"],
            checkpoint_spec=run["checkpoint_spec"],
            release_spec=spec,
        )
        head_after = _git(root, "rev-parse", "HEAD").decode().strip()
        if head_before != head_after:
            raise MemoryIntegrityError(
                "repository HEAD changed while preparing the release capability"
            )
        command = _pinned_release_command(run, repository_head=head_after)
        runner_command = [
            sys.executable,
            "-I",
            "-m",
            "comeback.runner",
            "--ready",
            str(ready_path),
            "--start",
            str(start_path),
            "--token",
            lock_record["nonce"],
            "--wait-seconds",
            str(max(120, min(timeout, spec["timeout_seconds"]))),
            "--",
            *command,
        ]
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(
            runner_command,
            cwd=root,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_options,
        )
        _wait_for_runner_ready(process, ready_path)
        process_birth_token = _required_process_birth_token(process.pid)
        lock_record = _update_release_lock(
            root,
            memory.repo_id,
            lock_record,
            child_process_id=process.pid,
            child_process_birth_token=process_birth_token,
            process_group_id=process.pid,
            child_started_at=utc_now(),
            phase="barrier_ready",
        )

        # The runner cannot execute the target yet. Recheck all captured state
        # after its durable identity is published, then make Sibyl's EXECUTING
        # transition durable before opening the one-shot start barrier.
        state_at_barrier = repository_fingerprint(
            root,
            run["state_policy"],
            checkpoint_spec=run["checkpoint_spec"],
            release_spec=spec,
        )
        if state_at_barrier != state_fingerprint:
            raise MemoryIntegrityError(
                "repository or captured release context changed before launch"
            )
        memory.begin_release(session_id, state_fingerprint=state_at_barrier)
        release_begun = True
        retain_lock = True
        lock_record = _update_release_lock(
            root,
            memory.repo_id,
            lock_record,
            phase="start_authorized",
        )
        _open_start_barrier(start_path, lock_record["nonce"])
        lock_record = _update_release_lock(
            root,
            memory.repo_id,
            lock_record,
            phase="child_started",
        )
        try:
            stdout, stderr = process.communicate(
                timeout=min(timeout, spec["timeout_seconds"])
            )
        except subprocess.TimeoutExpired as exc:
            raise MemoryIntegrityError("release capability timed out") from exc
        completed = subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )
        if os.name != "nt" and _posix_group_is_alive(process.pid):
            # The signed command returned while a descendant kept working.
            # Never accept that as a completed release.
            _stop_process_tree(process)
            raise MemoryIntegrityError(
                "release command left a background process; outcome is unknown"
            )
        lock_record = _update_release_lock(
            root,
            memory.repo_id,
            lock_record,
            phase="child_exited",
        )
    except BaseException as exc:
        # begin_release may have committed even if its caller observed an
        # exception. Re-read Sibyl before deciding whether this is a harmless
        # pre-launch failure or an outcome that must remain UNKNOWN.
        try:
            release_begun = release_begun or memory.get_run(session_id)[
                "status"
            ] != "open"
        except Exception:
            release_begun = True

        stop_error: BaseException | None = None
        if process is not None:
            try:
                _stop_process_tree(process)
            except Exception as tree_error:  # retain the lock on uncertainty
                stop_error = tree_error

        current_lock = read_release_lock(root, memory.repo_id)
        if current_lock is not None:
            lock_record = current_lock
            try:
                phase = (
                    "child_stopped"
                    if process is not None and stop_error is None
                    else "launch_failed"
                )
                lock_record = _update_release_lock(
                    root,
                    memory.repo_id,
                    lock_record,
                    phase=phase,
                )
            except Exception:
                # The complete existing lock remains the recovery authority.
                lock_record = current_lock

        if release_begun:
            retain_lock = True
            try:
                current_run = memory.get_run(session_id)
                if current_run["status"] == "executing":
                    memory.record_release_unknown(
                        session_id,
                        reason=(
                            "process_tree_not_stopped"
                            if stop_error is not None
                            else type(exc).__name__
                        ),
                    )
                current_lock = read_release_lock(root, memory.repo_id)
                if current_lock is not None:
                    lock_record = _update_release_lock(
                        root,
                        memory.repo_id,
                        current_lock,
                        phase="outcome_unknown_persisted",
                    )
            except Exception:
                # A retained stable lock plus an EXECUTING/UNKNOWN Sibyl run is
                # deliberately fail-closed even if the transition itself fails.
                pass
            detail = stop_error or exc
            raise MemoryIntegrityError(
                "release outcome is unknown after capability error; "
                f"operator reconciliation is required ({type(detail).__name__}: {detail})"
            ) from exc

        if isinstance(exc, MemoryIntegrityError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise MemoryIntegrityError(
            f"release capability could not start: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        ready_path.unlink(missing_ok=True)
        start_path.unlink(missing_ok=True)
        if not retain_lock:
            try:
                clear_release_lock(
                    root,
                    memory.repo_id,
                    session_id,
                    expected=lock_record,
                )
            except (FileNotFoundError, MemoryIntegrityError):
                pass
    if completed is None:  # pragma: no cover - all preceding branches return/raise
        raise AssertionError("release process completed without a result")
    if completed.returncode != 0:
        run = memory.record_release_unknown(
            session_id,
            reason=f"process_exit_{completed.returncode}",
        )
        lock_record = _update_release_lock(
            root,
            memory.repo_id,
            lock_record,
            phase="outcome_unknown_persisted",
        )
        result = _result(completed, run)
        result["decision"] = "release_outcome_unknown"
        result["outcome"] = "unknown"
        result["command"] = command
        return result, completed.returncode

    try:
        run = memory.record_release_outcome(session_id, success=True)
    except Exception as exc:
        try:
            memory.record_release_unknown(
                session_id,
                reason=f"outcome_persistence_{type(exc).__name__}",
            )
            lock_record = _update_release_lock(
                root,
                memory.repo_id,
                lock_record,
                phase="outcome_unknown_persisted",
            )
        except Exception:
            # The stable on-disk lock remains the fail-closed recovery marker even
            # if Sibyl itself cannot accept the UNKNOWN transition.
            pass
        raise MemoryIntegrityError(
            "release may have completed, but its outcome could not be persisted; "
            "operator reconciliation is required"
        ) from exc
    retain_lock = False
    clear_release_lock(
        root,
        memory.repo_id,
        session_id,
        expected=lock_record,
    )
    result = _result(completed, run)
    result["decision"] = "release_completed" if completed.returncode == 0 else "release_failed"
    result["outcome"] = run["outcome"]
    result["command"] = command
    return result, completed.returncode
