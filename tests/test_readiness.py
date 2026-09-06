"""Startup read faults must not become command retries or relaxed identity checks."""
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from comeback.execution import _wait_for_runner_ready, _run_contained_command
from comeback import execution
from comeback.memory import MemoryIntegrityError


def process():
    return SimpleNamespace(pid=123, poll=lambda: None)


@pytest.mark.parametrize('winerror', [32, 33])
def test_transient_windows_read_lock_retries_only_read(monkeypatch, tmp_path, winerror):
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            error = PermissionError(13, 'private diagnostic must not leak')
            error.winerror = winerror
            raise error
        return '{"process_id":123}'
    monkeypatch.setattr(execution, '_read_runner_ready', read)
    _wait_for_runner_ready(process(), tmp_path / 'ready')
    assert len(calls) == 2


@pytest.mark.parametrize('winerror', [None, 5])
def test_nontransient_read_error_is_not_retried(monkeypatch, tmp_path, winerror):
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        error = PermissionError(13, 'SECRET PATH')
        if winerror is not None:
            error.winerror = winerror
        raise error
    monkeypatch.setattr(execution, '_read_runner_ready', read)
    with pytest.raises(MemoryIntegrityError, match='read failed') as caught:
        _wait_for_runner_ready(process(), tmp_path / 'ready')
    assert len(calls) == 1
    assert 'errno=13' in str(caught.value)
    assert 'SECRET' not in str(caught.value)


@pytest.mark.parametrize('payload', ['', '{', '{"secret":"DO_NOT_PRINT",'])
def test_malformed_json_never_retries(monkeypatch, tmp_path, payload):
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        return payload
    monkeypatch.setattr(execution, '_read_runner_ready', read)
    with pytest.raises(MemoryIntegrityError, match='JSONDecodeError, line=') as caught:
        _wait_for_runner_ready(process(), tmp_path / 'ready')
    assert len(calls) == 1
    assert 'DO_NOT_PRINT' not in str(caught.value)


@pytest.mark.parametrize('payload', ['{}', '[]', '{"process_id":124}', '{"process_id":123.0}'])
def test_wrong_identity_is_never_accepted(tmp_path, payload):
    path = tmp_path / 'ready'
    path.write_text(payload)
    with pytest.raises(MemoryIntegrityError, match='identity is invalid'):
        _wait_for_runner_ready(process(), path)


def test_bad_encoding_has_safe_diagnostic(tmp_path):
    path = tmp_path / 'ready'
    path.write_bytes(b'\xff')
    with pytest.raises(MemoryIntegrityError, match='UTF-8 decoding failed'):
        _wait_for_runner_ready(process(), path)


def test_read_lock_has_bounded_deadline(monkeypatch, tmp_path):
    from comeback import execution
    times = iter([0, 0, 2])
    monkeypatch.setattr(execution.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(execution.time, 'sleep', lambda _: None)
    def read(*args, **kwargs):
        error = PermissionError(13, 'locked')
        error.winerror = 32
        raise error
    monkeypatch.setattr(execution, '_read_runner_ready', read)
    with pytest.raises(MemoryIntegrityError, match='last readiness read:.*winerror=32'):
        _wait_for_runner_ready(process(), tmp_path / 'ready', timeout=1)


def test_runner_exit_during_read_lock_is_terminal(monkeypatch, tmp_path):
    def read(*args, **kwargs):
        error = PermissionError(13, 'locked')
        error.winerror = 32
        raise error
    monkeypatch.setattr(execution, '_read_runner_ready', read)
    exited = SimpleNamespace(pid=123, returncode=126, poll=lambda: 126)
    with pytest.raises(MemoryIntegrityError, match='runner exited 126'):
        _wait_for_runner_ready(exited, tmp_path / 'ready')


@pytest.mark.parametrize('attempt', range(10))
def test_real_contained_startup_repeated(tmp_path, attempt):
    import sys
    marker = tmp_path / 'result'
    result = _run_contained_command(tmp_path, [sys.executable, '-c',
        "from pathlib import Path; Path('result').write_text('done')"], timeout=15)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == 'done'
    assert list((tmp_path / '.comeback/capability-runners').iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='requires actual Windows sharing semantics')
def test_actual_windows_exclusive_handle_read_recovers(tmp_path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    path = tmp_path / 'ready'
    path.write_text(json.dumps({'process_id': 123}), encoding='utf-8')
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        with pytest.raises(OSError) as caught:
            execution._read_runner_ready(path)
        assert caught.value.winerror == 32
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    closed = threading.Event()
    def unlock():
        try:
            threading.Event().wait(.2)
        finally:
            kernel.CloseHandle(handle)
            closed.set()
    worker = threading.Thread(target=unlock)
    worker.start()
    try:
        _wait_for_runner_ready(process(), path)
    finally:
        worker.join(timeout=2)
    assert closed.is_set()


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows read')
def test_native_windows_read_retains_access_denial(tmp_path):
    # Opening a directory as a normal data file must not be treated as a lock.
    with pytest.raises(MemoryIntegrityError, match='read failed.*winerror=5'):
        _wait_for_runner_ready(process(), tmp_path)


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows read')
def test_native_windows_read_caps_input_and_closes_handle(tmp_path):
    import ctypes
    from ctypes import wintypes
    path = tmp_path / 'ready'
    path.write_bytes(b'x' * 4097)
    with pytest.raises(MemoryIntegrityError, match='exceeds size limit'):
        execution._read_runner_ready(path)
    # FILE_SHARE_DELETE makes unlink insufficient to detect a leaked handle.
    # A new exclusive reader is incompatible with any still-open read handle.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    assert kernel.CloseHandle(handle)
    path.unlink()
