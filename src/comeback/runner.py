from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


_WINDOWS_JOB_SETTLE_SECONDS = 0.5
_WINDOWS_JOB_SETTLE_INTERVAL_SECONDS = 0.01


def _write_ready(path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps({"process_id": os.getpid()}) + "\n").encode("utf-8"),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _windows_containment_job():
    """Put this runner in a kill-on-close Job before it creates any target."""

    import ctypes
    from ctypes import wintypes

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        error = ctypes.get_last_error()
        raise OSError(error, "CreateJobObjectW failed")
    information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not kernel32.SetInformationJobObject(
        job,
        job_object_extended_limit_information,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    # Do not close this handle while the runner is alive. The operating system
    # closes it on runner exit, and KILL_ON_JOB_CLOSE then terminates every
    # surviving descendant. Children created below inherit this job before
    # their first user-mode instruction, so there is no Popen-to-assign gap.
    return kernel32, job


def _windows_job_active_processes(kernel32, job) -> int:
    """Count this runner and every live target descendant in its Job."""

    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    job_object_basic_accounting_information = 1
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    information = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    returned = wintypes.DWORD()
    if not kernel32.QueryInformationJobObject(
        job,
        job_object_basic_accounting_information,
        ctypes.byref(information),
        ctypes.sizeof(information),
        ctypes.byref(returned),
    ):
        raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
    return int(information.ActiveProcesses)


def _wait_for_windows_job_to_settle(kernel32, job) -> int:
    """Allow Windows a bounded interval to retire an exited target from its Job."""

    deadline = time.monotonic() + _WINDOWS_JOB_SETTLE_SECONDS
    while True:
        active_processes = _windows_job_active_processes(kernel32, job)
        if active_processes <= 1 or time.monotonic() >= deadline:
            return active_processes
        time.sleep(_WINDOWS_JOB_SETTLE_INTERVAL_SECONDS)


def run(
    ready: Path,
    start: Path,
    token: str,
    command: list[str],
    *,
    wait_seconds: int,
) -> int:
    if not command:
        raise ValueError("runner command is empty")
    kernel32 = None
    job = None
    if os.name == "nt":
        # Readiness on Windows means containment is already armed. The target
        # does not exist yet and cannot be orphaned outside the job.
        kernel32, job = _windows_containment_job()
    _write_ready(ready)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            start_value = start.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            start_value = None
        except OSError as exc:
            raise RuntimeError("start barrier is unreadable") from exc
        if start_value is not None:
            if start_value != token:
                raise RuntimeError("start barrier token is invalid")
            break
        if time.monotonic() >= deadline:
            return 125
        time.sleep(0.02)
    if os.name != "nt":
        os.execvpe(command[0], command, os.environ.copy())
        raise AssertionError("exec returned")  # pragma: no cover

    process = subprocess.Popen(command, shell=False)
    exit_code = process.wait()
    active_processes = _wait_for_windows_job_to_settle(kernel32, job)
    if active_processes > 1:
        # Returning 124 makes the outer capability record UNKNOWN. When this
        # runner exits, the still-open job handle closes and kills descendants.
        return 124
    if active_processes != 1:
        raise RuntimeError("Windows containment job lost the runner identity")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(prog="comeback-runner")
    parser.add_argument("--ready", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--wait-seconds", type=int, default=30)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        code = run(
            Path(args.ready),
            Path(args.start),
            args.token,
            command,
            wait_seconds=args.wait_seconds,
        )
    except Exception as exc:
        print(f"Comeback runner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 126
    raise SystemExit(code)


if __name__ == "__main__":
    main()
