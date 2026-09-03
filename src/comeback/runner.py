from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


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


def _windows_job_for(process: subprocess.Popen[bytes]):
    """Put the target tree in a kill-on-close Job Object on Windows."""

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
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
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
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        error = ctypes.get_last_error()
        process.terminate()
        process.wait(timeout=10)
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return kernel32, job


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume a process created suspended, after it is contained in its Job."""

    import ctypes
    from ctypes import wintypes

    # subprocess intentionally closes CreateProcess' primary-thread handle.
    # NtResumeProcess resumes every thread through the process handle that
    # Popen retains.  The target is still fully suspended until this call, so
    # it cannot escape before AssignProcessToJobObject succeeds.
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(wintypes.HANDLE(process._handle))
    if status != 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")


def _windows_job_has_active_processes(kernel32, job) -> bool:
    """Report whether target descendants remain after the leader exits."""

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
    return information.ActiveProcesses > 0


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

    # The Windows target is born suspended.  Only after it is assigned to a
    # kill-on-close Job Object may any of its instructions execute.  This
    # closes the launch/containment race that a normal Popen-then-assign flow
    # would leave open.
    create_suspended = 0x00000004
    process = subprocess.Popen(
        command,
        shell=False,
        creationflags=create_suspended | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    kernel32 = None
    job = None
    try:
        kernel32, job = _windows_job_for(process)
        try:
            _resume_windows_process(process)
        except Exception:
            kernel32.TerminateJobObject(job, 126)
            kernel32.CloseHandle(job)
            job = None
            process.wait(timeout=10)
            raise
        exit_code = process.wait()
        if _windows_job_has_active_processes(kernel32, job):
            kernel32.TerminateJobObject(job, 124)
            return 124
        return exit_code
    finally:
        if job is not None and kernel32 is not None:
            kernel32.CloseHandle(job)


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
