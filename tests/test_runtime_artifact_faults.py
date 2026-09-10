"""Control-file faults must not become target retries or invented outcomes."""

import errno
import json
import os
import threading
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from comeback import execution, runner
from comeback.memory import MemoryIntegrityError
from test_execution import _approve, _supervised_memory


def _sharing_error(code=32):
    error = PermissionError(errno.EACCES, "private path must not appear")
    error.winerror = code
    return error


@pytest.fixture
def target_calls(monkeypatch):
    """Drive both platform branches without executing or containing pytest."""
    calls = []
    monkeypatch.setattr(runner, "_write_ready", lambda _path: None)
    monkeypatch.setattr(runner, "_windows_containment_job", lambda: (None, None))
    monkeypatch.setattr(runner, "_wait_for_windows_job_to_settle", lambda *_: 1)

    def exec_target(_executable, command, _environment):
        calls.append(command)
        raise SystemExit(0)

    def popen_target(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(runner.os, "execvpe", exec_target)
    monkeypatch.setattr(runner.subprocess, "Popen", popen_target)
    return calls


def _run_stub():
    try:
        return runner.run(Path("ready"), Path("start"), "token", ["target"], wait_seconds=1)
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize("code", [32, 33])
def test_start_barrier_retries_only_transient_read(monkeypatch, target_calls, code):
    reads = []

    def read(_path):
        reads.append(1)
        if len(reads) == 1:
            raise _sharing_error(code)
        return b"token\n"

    monkeypatch.setattr(runner, "_read_control_bytes", read)
    assert _run_stub() == 0
    assert len(reads) == 2
    assert target_calls == [["target"]]


@pytest.mark.parametrize("code", [5, None])
def test_start_barrier_does_not_retry_access_denial(monkeypatch, target_calls, code):
    reads = []

    def read(_path):
        reads.append(1)
        raise _sharing_error(code)

    monkeypatch.setattr(runner, "_read_control_bytes", read)
    with pytest.raises(RuntimeError, match="start barrier is unreadable") as caught:
        _run_stub()
    assert len(reads) == 1
    assert "private path" not in str(caught.value)
    assert target_calls == []


def test_start_barrier_transient_reads_have_one_deadline(monkeypatch, target_calls):
    clock = iter([0, 0, .25, 1.1])
    reads = []

    def read(_path):
        reads.append(1)
        raise _sharing_error()

    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "_read_control_bytes", read)
    with pytest.raises(RuntimeError, match="timed out.*winerror=32"):
        _run_stub()
    assert len(reads) == 2
    assert target_calls == []


def test_start_barrier_does_not_retry_a_valid_read_after_deadline(monkeypatch, target_calls):
    clock = [0.0]
    reads = []

    def read(_path):
        reads.append(clock[0])
        if len(reads) == 1:
            clock[0] = .99
            raise _sharing_error()
        return b"token\n"

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.time, "sleep", lambda _: clock.__setitem__(0, 1.01))
    monkeypatch.setattr(runner, "_read_control_bytes", read)
    with pytest.raises(RuntimeError, match="timed out.*winerror=32"):
        _run_stub()
    assert reads == [0.0]
    assert target_calls == []


def test_start_barrier_does_not_accept_a_read_completed_after_deadline(monkeypatch, target_calls):
    clock = [0.0]
    reads = []

    def read(_path):
        reads.append(clock[0])
        clock[0] = 1.01
        return b"token\n"

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner, "_read_control_bytes", read)
    assert _run_stub() == 125
    assert reads == [0.0]
    assert target_calls == []


@pytest.mark.parametrize("payload", [b"", b"tok", b"wrong\n", b"token\nextra"])
def test_start_barrier_invalid_token_never_launches(monkeypatch, target_calls, payload):
    reads = []

    def read(_path):
        reads.append(1)
        return payload

    monkeypatch.setattr(runner, "_read_control_bytes", read)
    with pytest.raises(RuntimeError, match="token is invalid"):
        _run_stub()
    assert len(reads) == 1
    assert target_calls == []


def test_start_barrier_oversized_record_never_launches(monkeypatch, target_calls):
    def read(_path):
        raise ValueError("control record exceeds size limit")

    monkeypatch.setattr(runner, "_read_control_bytes", read)
    with pytest.raises(RuntimeError, match="exceeds size limit"):
        _run_stub()
    assert target_calls == []


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows sharing semantics")
def test_native_start_barrier_sharing_violation_recovers_once(tmp_path, monkeypatch, target_calls):
    import ctypes
    from ctypes import wintypes

    start = tmp_path / "start"
    start.write_bytes(b"token\n")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(start), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    original_read = runner._read_control_bytes
    try:
        with pytest.raises(OSError) as caught:
            original_read(start)
        assert caught.value.winerror == 32
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    reads = []
    blocked_read_seen = threading.Event()
    closed = threading.Event()

    def read(path):
        try:
            result = original_read(path)
        except OSError as exc:
            reads.append(getattr(exc, "winerror", None))
            blocked_read_seen.set()
            raise
        reads.append("token")
        return result

    def unlock():
        try:
            blocked_read_seen.wait(2)
        finally:
            kernel.CloseHandle(handle)
            closed.set()

    monkeypatch.setattr(runner, "_read_control_bytes", read)
    worker = threading.Thread(target=unlock)
    worker.start()
    try:
        assert runner.run(tmp_path / "ready", start, "token", ["target"], wait_seconds=3) == 0
    finally:
        worker.join(timeout=3)
    assert closed.is_set()
    assert reads[0] == 32
    assert reads[-1] == "token"
    assert target_calls == [["target"]]


def test_control_read_is_bounded_and_closes_file(tmp_path):
    path = tmp_path / "record"
    path.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError, match="size limit"):
        runner._read_control_bytes(path)
    path.unlink()


@pytest.mark.parametrize("suffix", ["ready", "start", "tmp"])
def test_scratch_cleanup_failure_preserves_success_and_one_shot(tmp_path, monkeypatch, capsys, suffix):
    memory, owner = _supervised_memory(tmp_path)
    try:
        execution.execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        _approve(memory, owner)
        original_unlink = Path.unlink
        faults = []

        def unlink(path, *args, **kwargs):
            if path.name.endswith("." + suffix):
                faults.append(path)
                raise _sharing_error()
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        result, code = execution.execute_release(memory, session_id="fresh", root=tmp_path)
        assert faults
        assert code == 0
        assert result["outcome"] == "success"
        assert (tmp_path / "released.txt").read_text() == "ok"
        assert memory.get_run("fresh")["status"] == "completed"
        assert execution.read_release_lock(tmp_path, memory.repo_id) is None
        with pytest.raises(MemoryIntegrityError, match="not open"):
            execution.execute_release(memory, session_id="fresh", root=tmp_path)
        diagnostic = capsys.readouterr().err
        assert "scratch cleanup deferred" in diagnostic
        assert "private path" not in diagnostic
    finally:
        memory.close()


def test_scratch_cleanup_failure_preserves_unknown_result(tmp_path, monkeypatch):
    import sys

    memory, owner = _supervised_memory(
        tmp_path, release_argv=[sys.executable, "-c", "raise SystemExit(7)"]
    )
    try:
        execution.execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        _approve(memory, owner)
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.suffix in {".ready", ".start"}:
                raise _sharing_error()
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        result, code = execution.execute_release(memory, session_id="fresh", root=tmp_path)
        assert code == 7
        assert result["outcome"] == "unknown"
        assert memory.get_run("fresh")["status"] == "unknown"
        assert execution.read_release_lock(tmp_path, memory.repo_id)["phase"] == "outcome_unknown_persisted"
        assert not (tmp_path / "released.txt").exists()
    finally:
        memory.close()


def test_cleanup_failure_preserves_prelaunch_refusal_and_clears_reservation(tmp_path, monkeypatch):
    memory, owner = _supervised_memory(tmp_path)
    try:
        execution.execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        _approve(memory, owner)
        original_unlink = Path.unlink

        def refuse_readiness(*_args, **_kwargs):
            raise MemoryIntegrityError("original readiness refusal")

        def unlink(path, *args, **kwargs):
            if path.suffix in {".ready", ".start"}:
                raise _sharing_error()
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(execution, "_wait_for_runner_ready", refuse_readiness)
        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(MemoryIntegrityError, match="original readiness refusal"):
            execution.execute_release(memory, session_id="fresh", root=tmp_path)
        assert memory.get_run("fresh")["status"] == "open"
        assert execution.read_release_lock(tmp_path, memory.repo_id) is None
        assert not (tmp_path / "released.txt").exists()
    finally:
        memory.close()


def test_runner_cleanup_cannot_mask_publication_error(tmp_path, monkeypatch):
    ready = tmp_path / "ready"

    def replace(*_args):
        raise OSError(errno.EIO, "original publication failure")

    def unlink(*_args, **_kwargs):
        raise _sharing_error()

    monkeypatch.setattr(runner.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="original publication failure"):
        runner._write_ready(ready)
    assert not ready.exists()
    assert runner._scratch_path(ready, "", "ready-tmp").exists()


def test_ready_cleanup_cannot_mask_successful_publication(tmp_path, monkeypatch, capsys):
    ready = tmp_path / "ready"

    def unlink(*_args, **_kwargs):
        raise _sharing_error()

    monkeypatch.setattr(Path, "unlink", unlink)
    runner._write_ready(ready)
    assert json.loads(ready.read_text()) == {"process_id": os.getpid()}
    assert "scratch cleanup deferred" in capsys.readouterr().err


def test_contained_cleanup_does_not_replace_known_result(tmp_path, monkeypatch):
    import sys

    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.suffix in {".ready", ".start"}:
            raise _sharing_error()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = execution._run_contained_command(
        tmp_path, [sys.executable, "-c", "print('one successful check')"], timeout=15
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "one successful check"


@pytest.mark.parametrize("operation", ["publish", "start", "ready"])
def test_staging_collision_never_deletes_unowned_file(tmp_path, operation):
    path = tmp_path / "record"
    token = "token"
    if operation == "publish":
        stage = runner._scratch_path(path, token, "lock-tmp")
        invoke = lambda: execution._publish_lock(path, {"nonce": token})
    elif operation == "start":
        stage = execution._start_barrier_temporary(path, token)
        invoke = lambda: execution._open_start_barrier(path, token)
    else:
        stage = runner._scratch_path(path, "", "ready-tmp")
        invoke = lambda: runner._write_ready(path)
    stage.write_bytes(b"unowned")
    with pytest.raises(FileExistsError):
        invoke()
    assert stage.read_bytes() == b"unowned"
    assert not path.exists()


def test_all_generated_names_fit_when_stable_lock_fits():
    root = PureWindowsPath("C:/Users/test/" + "d" * 116)
    lock = root / ".comeback" / "release-locks" / ("a" * 64 + ".lock")
    nonce = "b" * 32
    ready, start = execution._barrier_paths(lock, nonce)
    auxiliary = [
        runner._scratch_path(lock, nonce, "lock-tmp"),
        runner._scratch_path(lock, nonce, "lock-update"),
        ready, start,
        runner._scratch_path(ready, "", "ready-tmp"),
        execution._start_barrier_temporary(start, nonce),
    ]
    assert len(str(lock)) == 224
    assert len(set(auxiliary)) == len(auxiliary)
    assert all(len(path.name) <= len(lock.name) for path in auxiliary)
    assert all(len(str(path)) < 260 for path in auxiliary)
    assert execution._barrier_paths(lock, "c" * 32) != (ready, start)
    checkpoint_ready = root / (nonce + ".ready")
    checkpoint_start = root / (nonce + ".start")
    assert len(runner._scratch_path(checkpoint_ready, "", "ready-tmp").name) <= len(checkpoint_ready.name)
    assert len(execution._start_barrier_temporary(checkpoint_start, nonce).name) <= len(checkpoint_start.name)


def test_stable_lock_publication_error_remains_fatal(tmp_path, monkeypatch):
    lock = tmp_path / "stable.lock"

    def link(*_args):
        raise OSError(errno.EIO, "authoritative publication failed")

    monkeypatch.setattr(execution.os, "link", link)
    with pytest.raises(MemoryIntegrityError, match="could not be published atomically"):
        execution._publish_lock(lock, {"nonce": "token"})
    assert not lock.exists()


def test_stable_lock_publication_fault_is_not_masked_by_cleanup(tmp_path, monkeypatch):
    lock = tmp_path / "stable.lock"

    def link(*_args):
        raise OSError(errno.EIO, "authoritative publication failed")

    def unlink(*_args, **_kwargs):
        raise _sharing_error()

    monkeypatch.setattr(execution.os, "link", link)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(MemoryIntegrityError, match="could not be published atomically") as caught:
        execution._publish_lock(lock, {"nonce": "token"})
    assert str(caught.value.__cause__) == "[Errno 5] authoritative publication failed"
    assert not lock.exists()
    assert runner._scratch_path(lock, "token", "lock-tmp").exists()


def test_completed_outcome_does_not_make_authoritative_lock_removal_optional(tmp_path, monkeypatch):
    memory, owner = _supervised_memory(tmp_path)
    try:
        execution.execute_checkpoint(memory, session_id="fresh", root=tmp_path)
        _approve(memory, owner)
        stable_lock = execution.release_lock_path(tmp_path, memory.repo_id)
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == stable_lock:
                raise _sharing_error()
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError):
            execution.execute_release(memory, session_id="fresh", root=tmp_path)
        assert memory.get_run("fresh")["status"] == "completed"
        assert memory.get_run("fresh")["outcome"] == "success"
        assert (tmp_path / "released.txt").read_text() == "ok"
        assert stable_lock.exists()
        with pytest.raises(MemoryIntegrityError, match="not open"):
            execution.execute_release(memory, session_id="fresh", root=tmp_path)
    finally:
        memory.close()


def test_stable_lock_update_error_is_not_auxiliary_cleanup(tmp_path, monkeypatch):
    record, path = execution._release_lock(tmp_path, "repo-a", "fresh")

    def replace(*_args):
        raise OSError(errno.EIO, "authoritative update failed")

    monkeypatch.setattr(execution.os, "replace", replace)
    with pytest.raises(OSError, match="authoritative update failed"):
        execution._update_release_lock(tmp_path, "repo-a", record, phase="barrier_ready")
    assert json.loads(path.read_text()) == record
