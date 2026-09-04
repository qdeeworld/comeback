import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _runner_command(
    ready: Path,
    start: Path,
    token: str,
    command: list[str],
) -> list[str]:
    from comeback.execution import _runner_launch_command

    return _runner_launch_command(
        ready=ready,
        start=start,
        token=token,
        wait_seconds=20,
        command=command,
    )


def _wait_for(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def test_runner_cannot_execute_target_before_start_barrier(tmp_path: Path):
    ready = tmp_path / "runner.ready"
    start = tmp_path / "runner.start"
    marker = tmp_path / "target-ran.txt"
    token = "single-use-test-token"
    command = _runner_command(
        ready,
        start,
        token,
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(ready)
        assert json.loads(ready.read_text(encoding="utf-8")) == {
            "process_id": process.pid
        }
        time.sleep(0.15)
        assert not marker.exists()

        start.write_text(token + "\n", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
        assert marker.read_text(encoding="utf-8") == "ran"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_runner_rejects_wrong_start_token(tmp_path: Path):
    ready = tmp_path / "runner.ready"
    start = tmp_path / "runner.start"
    marker = tmp_path / "target-ran.txt"
    process = subprocess.Popen(
        _runner_command(
            ready,
            start,
            "expected",
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(ready)
        start.write_text("wrong\n", encoding="utf-8")
        _, stderr = process.communicate(timeout=20)
        assert process.returncode == 126
        assert "start barrier token is invalid" in stderr
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_windows_job_settling_rechecks_a_transient_process_count(monkeypatch):
    from comeback import runner

    counts = iter([2, 2, 1])
    monkeypatch.setattr(
        runner,
        "_windows_job_active_processes",
        lambda _kernel32, _job: next(counts),
    )
    monkeypatch.setattr(runner, "_WINDOWS_JOB_SETTLE_INTERVAL_SECONDS", 0)

    assert runner._wait_for_windows_job_to_settle(object(), object()) == 1


def test_windows_job_settling_stops_at_its_deadline(monkeypatch):
    from comeback import runner

    monotonic_values = iter([10.0, 10.6])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        runner,
        "_windows_job_active_processes",
        lambda _kernel32, _job: 2,
    )

    assert runner._wait_for_windows_job_to_settle(object(), object()) == 2


if os.name == "nt":

    def test_windows_runner_pid_matches_from_a_redirecting_virtualenv(tmp_path: Path):
        if sys.prefix != sys.base_prefix:
            pytest.skip("the current test process already runs from a virtual environment")

        import venv

        environment = tmp_path / "redirecting-venv"
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
        venv_python = environment / "Scripts" / "python.exe"
        probe = tmp_path / "probe_runner_identity.py"
        ready = tmp_path / "venv-runner.ready"
        start = tmp_path / "venv-runner.start"
        marker = tmp_path / "venv-target-ran.txt"
        probe.write_text(
            """
import json
import subprocess
import sys
import time
from pathlib import Path

from comeback.execution import _runner_launch_command

ready, start, marker = map(Path, sys.argv[1:])
assert sys.prefix != sys.base_prefix
target = [
    sys.executable,
    "-c",
    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
]
command = _runner_launch_command(
    ready=ready,
    start=start,
    token="go",
    wait_seconds=20,
    command=target,
)
process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
deadline = time.monotonic() + 10
while not ready.is_file() and time.monotonic() < deadline:
    if process.poll() is not None:
        break
    time.sleep(0.02)
assert ready.is_file(), process.communicate(timeout=5)
assert json.loads(ready.read_text(encoding="utf-8")) == {
    "process_id": process.pid
}
start.write_text("go\\n", encoding="utf-8")
stdout, stderr = process.communicate(timeout=20)
assert process.returncode == 0, (stdout, stderr)
assert marker.read_text(encoding="utf-8") == "ran"
""".lstrip(),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [venv_python, str(probe), str(ready), str(start), str(marker)],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr

    def test_windows_runner_containment_failure_never_signals_readiness_or_starts_target(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from comeback import runner

        ready = tmp_path / "runner.ready"
        start = tmp_path / "runner.start"
        marker = tmp_path / "target-ran.txt"
        start.write_text("go\n", encoding="utf-8")

        def fail_containment_setup():
            raise OSError("simulated Job setup failure")

        monkeypatch.setattr(runner, "_windows_containment_job", fail_containment_setup)

        with pytest.raises(OSError, match="simulated Job setup failure"):
            runner.run(
                ready,
                start,
                "go",
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                ],
                wait_seconds=1,
            )

        assert not ready.exists()
        assert not marker.exists()

    def test_windows_runner_marks_daemonizing_target_unknown(tmp_path: Path):
        ready = tmp_path / "runner.ready"
        start = tmp_path / "runner.start"
        late_marker = tmp_path / "late.txt"
        grandchild_code = (
            "import time; from pathlib import Path; time.sleep(2); "
            f"Path({str(late_marker)!r}).write_text('late')"
        )
        target_code = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
            "raise SystemExit(0)"
        )
        process = subprocess.Popen(
            _runner_command(
                ready,
                start,
                "go",
                [sys.executable, "-c", target_code],
            )
        )
        _wait_for(ready)
        start.write_text("go\n", encoding="utf-8")
        process.wait(timeout=20)
        assert process.returncode == 124
        time.sleep(2.5)
        assert not late_marker.exists()

    def test_windows_runner_job_kills_target_tree_with_runner(tmp_path: Path):
        ready = tmp_path / "runner.ready"
        start = tmp_path / "runner.start"
        child_ready = tmp_path / "child.ready"
        late_marker = tmp_path / "late.txt"
        grandchild_code = (
            "import time; from pathlib import Path; time.sleep(2); "
            f"Path({str(late_marker)!r}).write_text('late')"
        )
        child_code = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"Path({str(child_ready)!r}).write_text('ready'); "
            f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
            "time.sleep(30)"
        )
        process = subprocess.Popen(
            _runner_command(
                ready,
                start,
                "go",
                [sys.executable, "-c", child_code],
            )
        )
        _wait_for(ready)
        start.write_text("go\n", encoding="utf-8")
        _wait_for(child_ready)
        process.kill()
        process.wait(timeout=10)
        time.sleep(2.5)
        assert not late_marker.exists()
