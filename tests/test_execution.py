import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.execution import (
    _open_start_barrier,
    _start_barrier_temporary,
    _process_birth_token,
    _required_process_birth_token,
    _resolved_executable,
    _release_lock,
    execute_checkpoint,
    execute_release,
    read_release_lock,
    reconcile_release,
    release_lock_path,
)
import comeback.execution as execution_module
import comeback.memory as memory_module
from comeback.memory import InterventionMemory, MemoryIntegrityError, release_destination
from comeback.signing import (
    action_spec_digest,
    approval_message,
    checkpoint_receipt_digest,
    intervention_message,
    reconciliation_message,
)


def _supervised_memory(
    root: Path,
    *,
    checkpoint_exit: int = 0,
    checkpoint_argv: list[str] | None = None,
    checkpoint_timeout: int = 60,
    require_clean_git: bool = True,
    release_argv: list[str] | None = None,
) -> tuple[InterventionMemory, object]:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Comeback Test"], check=True)
    (root / ".gitignore").write_text(".comeback/\nreleased.txt\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True)
    memory = InterventionMemory(root / ".comeback" / "memory.db", "repo-a")
    owner = Account.create()
    checkpoint = checkpoint_argv or [
        sys.executable,
        "-c",
        f"print('checkpoint'); raise SystemExit({checkpoint_exit})",
    ]
    release = release_argv or [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('released.txt').write_text('ok')",
    ]
    fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": "repo-a",
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "agent_scope": "all_supported",
        "severity": "release_blocker",
        "action_schema": 2,
        "checkpoint_spec": {
            "argv": checkpoint,
            "timeout_seconds": checkpoint_timeout,
        },
        "release_spec": {"argv": release, "timeout_seconds": 60},
        "state_policy": {
            "bind_head": True,
            "require_clean_git": require_clean_git,
        },
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "source",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    memory.start_run(
        session_id="source",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test-source",
    )
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(fields)), private_key=owner.key
    ).signature.hex()
    memory.record_intervention(
        {
            "signed_fields": fields,
            "intervention_signature": signature,
            "incident_summary": "Skipped checkpoint.",
        }
    )
    memory.start_run(
        session_id="fresh",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    return memory, owner


def test_start_barrier_is_not_visible_until_token_is_complete(
    tmp_path: Path, monkeypatch
):
    barrier = tmp_path / "start"
    token = "atomic-token"
    original_write = os.write
    write_started = threading.Event()

    def delayed_write(descriptor: int, value: bytes) -> int:
        write_started.set()
        time.sleep(0.15)
        return original_write(descriptor, value)

    monkeypatch.setattr(os, "write", delayed_write)
    publisher = threading.Thread(target=_open_start_barrier, args=(barrier, token))
    publisher.start()
    assert write_started.wait(timeout=5)
    assert not barrier.exists()
    publisher.join(timeout=5)
    assert not publisher.is_alive()
    assert barrier.read_text(encoding="utf-8") == token + "\n"


def test_start_barrier_retries_partial_writes(tmp_path: Path, monkeypatch) -> None:
    barrier = tmp_path / "start"
    token = "partial-write-token"
    original_write = os.write
    writes = 0

    def partial_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        return original_write(descriptor, payload[:2])

    monkeypatch.setattr(os, "write", partial_write)

    _open_start_barrier(barrier, token)

    assert barrier.read_bytes() == (token + "\n").encode("utf-8")
    assert writes > 1
    assert not _start_barrier_temporary(barrier, token).exists()


def test_start_barrier_removes_temporary_after_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    barrier = tmp_path / "start"
    token = "failed-write-token"

    def fail_write(_descriptor: int, _payload: bytes) -> int:
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(OSError, match="simulated write failure"):
        _open_start_barrier(barrier, token)

    assert not barrier.exists()
    assert not _start_barrier_temporary(barrier, token).exists()


def test_start_barrier_does_not_remove_unowned_temporary_file(tmp_path: Path) -> None:
    barrier = tmp_path / "start"
    token = "occupied-token"
    temporary = _start_barrier_temporary(barrier, token)
    temporary.write_text("preexisting", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _open_start_barrier(barrier, token)

    assert temporary.read_text(encoding="utf-8") == "preexisting"
    assert not barrier.exists()


def test_start_barrier_fits_when_legacy_staging_exceeds_windows_path_limit(
    tmp_path: Path, monkeypatch
) -> None:
    token = "a" * 32
    directory = tmp_path / ("d" * max(1, 145 - len(str(tmp_path))))
    directory.mkdir()
    barrier = directory / ("." + "b" * 64 + ".lock." + token + ".start")
    legacy = barrier.with_name(f".{barrier.name}.{token}.tmp")
    # Exercise the Windows path budget deterministically on every CI host.
    limit = 260
    assert len(str(barrier)) < limit
    assert len(str(legacy)) >= limit
    assert len(str(_start_barrier_temporary(barrier, token))) < limit
    original_open = os.open

    def bounded_open(path, *args, **kwargs):
        if len(str(path)) >= limit:
            raise FileNotFoundError(2, "simulated Windows path limit", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", bounded_open)
    with pytest.raises(FileNotFoundError):
        os.open(legacy, execution_module._ATOMIC_WRITE_FLAGS, 0o600)
    _open_start_barrier(barrier, token)
    assert barrier.read_bytes() == (token + "\n").encode()
    assert not _start_barrier_temporary(barrier, token).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native process identity")
def test_macos_process_birth_token_does_not_spawn_ps(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("macOS process identity must not launch ps")
        ),
    )
    token = _process_birth_token(os.getpid())
    assert token is not None
    assert token.startswith("darwin-starttime:")


def test_unqualified_executable_is_resolved_against_action_repository(
    tmp_path: Path, monkeypatch
):
    repository = tmp_path / "repository"
    caller = tmp_path / "caller"
    repository.mkdir()
    caller.mkdir()
    name = "release-tool.exe" if os.name == "nt" else "release-tool"
    repository_tool = repository / name
    caller_tool = caller / name
    repository_tool.write_bytes(b"repository")
    caller_tool.write_bytes(b"caller")
    if os.name != "nt":
        repository_tool.chmod(0o755)
        caller_tool.chmod(0o755)
    monkeypatch.chdir(caller)
    monkeypatch.setattr(os, "get_exec_path", lambda *_args, **_kwargs: ["."])

    assert _resolved_executable(repository, name) == Path(
        os.path.abspath(repository_tool)
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher semantics")
def test_resolved_executable_preserves_symlink_launcher_path(tmp_path: Path):
    launcher = tmp_path / "python-in-venv"
    launcher.symlink_to(Path(sys.executable))

    selected = _resolved_executable(tmp_path, str(launcher))

    assert selected == Path(os.path.abspath(launcher))
    assert selected != launcher.resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows batch dispatch semantics")
def test_windows_unqualified_batch_executable_is_rejected(tmp_path: Path, monkeypatch):
    batch = tmp_path / "deploy.cmd"
    batch.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD;.BAT")
    monkeypatch.setattr(os, "get_exec_path", lambda *_args, **_kwargs: [str(tmp_path)])

    with pytest.raises(MemoryIntegrityError, match="batch files"):
        _resolved_executable(tmp_path, "deploy")


@pytest.mark.skipif(os.name != "nt", reason="Windows PATHEXT semantics")
def test_windows_native_executable_survives_truncated_pathext(tmp_path: Path, monkeypatch):
    executable = tmp_path / "release-tool.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setenv("PATHEXT", ".CPL")
    monkeypatch.setattr(os, "get_exec_path", lambda *_args, **_kwargs: [str(tmp_path)])

    expected = Path(os.path.abspath(executable))
    assert _resolved_executable(tmp_path, str(executable)) == expected
    assert _resolved_executable(tmp_path, "release-tool") == expected


def test_deep_path_checkpoint_and_release_capabilities():
    # Exercise a realistic Windows user/temp prefix, not only CI's short cwd.
    with tempfile.TemporaryDirectory(prefix="cb-path-") as temporary:
        root = Path(temporary) / ("r" * max(1, 120 - len(temporary) - 1))
        root.mkdir()
        test_cross_platform_checkpoint_and_release_capabilities(root)


def test_cross_platform_checkpoint_and_release_capabilities(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    with memory:
        _assert_checkpoint_and_release(tmp_path, memory, owner)


def _assert_checkpoint_and_release(tmp_path: Path, memory, owner):
    checkpoint, checkpoint_exit = execute_checkpoint(
        memory, session_id="fresh", root=tmp_path
    )
    assert checkpoint_exit == 0
    assert checkpoint["decision"] == "checkpoint_recorded"
    assert checkpoint["remaining"] == ["human_approval"]

    with pytest.raises(MemoryIntegrityError, match="human_approval"):
        execute_release(
            memory,
            session_id="fresh",
            root=tmp_path,
        )

    run = memory.get_run("fresh")
    approved_at = datetime.now(timezone.utc).isoformat()
    approval = Account.sign_message(
        encode_defunct(text=approval_message(run, approved_at)), private_key=owner.key
    ).signature.hex()
    memory.approve("fresh", approved_at=approved_at, signature=approval)
    marker = tmp_path / "released.txt"
    release, release_exit = execute_release(
        memory,
        session_id="fresh",
        root=tmp_path,
    )
    assert release_exit == 0
    assert release["outcome"] == "success"
    assert marker.read_text() == "ok"
    assert memory.get_run("fresh")["status"] == "completed"


def test_repository_cannot_shadow_the_trusted_release_runner(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path, require_clean_git=False)
    shadow_marker = tmp_path / "shadow-runner-imported.txt"
    shadow = tmp_path / "comeback"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(shadow_marker)!r}).write_text('shadowed')\n",
        encoding="utf-8",
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)

    result, exit_code = execute_release(memory, session_id="fresh", root=tmp_path)

    assert exit_code == 0
    assert result["outcome"] == "success"
    assert not shadow_marker.exists()


def test_failed_checkpoint_never_records_evidence(tmp_path: Path):
    memory, _ = _supervised_memory(tmp_path, checkpoint_exit=7)
    result, exit_code = execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    assert exit_code == 7
    assert result["decision"] == "checkpoint_failed"
    assert "release_check_passed" not in memory.get_run("fresh")["satisfied_evidence"]


def test_checkpoint_defaults_to_signed_timeout_above_ten_minutes(
    tmp_path: Path, monkeypatch
):
    memory, _ = _supervised_memory(tmp_path, checkpoint_timeout=1200)
    observed: list[int] = []

    def fake_run(_root, command, *, timeout):
        observed.append(timeout)
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    monkeypatch.setattr(execution_module, "_run_contained_command", fake_run)

    result, exit_code = execute_checkpoint(memory, session_id="fresh", root=tmp_path)

    assert exit_code == 0
    assert result["decision"] == "checkpoint_recorded"
    assert observed == [1200]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_checkpoint_never_mints_receipt_while_descendant_survives(tmp_path: Path):
    marker = tmp_path / "late-checkpoint-child.txt"
    child_code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(2); "
        f"Path({str(marker)!r}).write_text('late')"
    )
    checkpoint_code = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "raise SystemExit(0)"
    )
    memory, _ = _supervised_memory(
        tmp_path,
        checkpoint_argv=[sys.executable, "-c", checkpoint_code],
    )

    with pytest.raises(MemoryIntegrityError, match="background process"):
        execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    time.sleep(2.2)
    run = memory.get_run("fresh")
    assert "release_check_passed" not in run["satisfied_evidence"]
    assert run["checkpoint_receipt"] is None
    assert not marker.exists()


def test_self_hashed_receipt_for_another_command_is_rejected(tmp_path: Path):
    memory, _ = _supervised_memory(tmp_path, checkpoint_exit=99)
    run = memory.get_run("fresh")
    now = datetime.now(timezone.utc).isoformat()
    forged = {
        "repo_id": "repo-a",
        "session_id": "fresh",
        "checkpoint_spec_sha256": "0" * 64,
        "release_spec_sha256": action_spec_digest(run["release_spec"]),
        "state_fingerprint": "forged",
        "repository_head": subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "repository_branch": "fixture",
        "release_destination": release_destination(run["release_spec"]),
        "started_at": now,
        "completed_at": now,
        "exit_code": 0,
    }
    forged["digest"] = checkpoint_receipt_digest(forged)
    attempt_id = "forged-receipt-attempt"
    memory.begin_checkpoint_attempt(
        "fresh",
        attempt_id=attempt_id,
        started_at=now,
    )

    with pytest.raises(MemoryIntegrityError, match="signed command"):
        memory.record_checkpoint_receipt(
            "fresh",
            forged,
            attempt_id=attempt_id,
        )
    assert memory.get_run("fresh")["satisfied_evidence"] == []


def _approve(memory: InterventionMemory, owner: object, session_id: str = "fresh") -> None:
    run = memory.get_run(session_id)
    approved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(text=approval_message(run, approved_at)),
        private_key=owner.key,
    ).signature.hex()
    memory.approve(session_id, approved_at=approved_at, signature=signature)


def test_repository_change_after_checkpoint_invalidates_release(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path, require_clean_git=False)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    (tmp_path / ".gitignore").write_text(
        ".comeback/\nreleased.txt\nchanged.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(MemoryIntegrityError, match="state changed"):
        execute_release(memory, session_id="fresh", root=tmp_path)
    assert not (tmp_path / "released.txt").exists()


def test_second_checkpoint_invalidates_earlier_human_approval(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    assert memory.missing_requirements(memory.get_run("fresh")) == []

    execute_checkpoint(memory, session_id="fresh", root=tmp_path)

    assert memory.missing_requirements(memory.get_run("fresh")) == ["human_approval"]
    with pytest.raises(MemoryIntegrityError, match="human_approval"):
        execute_release(memory, session_id="fresh", root=tmp_path)


def test_approval_journal_outage_does_not_report_committed_authority_as_failed(
    tmp_path: Path, monkeypatch
):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)

    def fail_journal(**_kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(memory.client, "write_event", fail_journal)
    _approve(memory, owner)

    run = memory.get_verified_run("fresh")
    assert "human_approval" in run["satisfied_evidence"]
    assert memory.missing_requirements(run) == []


def test_failed_recheck_revokes_earlier_receipt_and_approval(tmp_path: Path):
    failure_flag = tmp_path / ".comeback" / "fail-checkpoint"
    checkpoint_code = (
        "from pathlib import Path; "
        "raise SystemExit(7 if Path('.comeback/fail-checkpoint').exists() else 0)"
    )
    memory, owner = _supervised_memory(
        tmp_path,
        checkpoint_argv=[sys.executable, "-c", checkpoint_code],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    assert memory.missing_requirements(memory.get_run("fresh")) == []

    failure_flag.parent.mkdir(parents=True, exist_ok=True)
    failure_flag.write_text("fail\n", encoding="utf-8")
    result, exit_code = execute_checkpoint(memory, session_id="fresh", root=tmp_path)

    run = memory.get_run("fresh")
    assert exit_code == 7
    assert result["decision"] == "checkpoint_failed"
    assert run["checkpoint_receipt"] is None
    assert run.get("approval") is None
    assert memory.missing_requirements(run) == [
        "human_approval",
        "release_check_passed",
    ]
    with pytest.raises(MemoryIntegrityError, match="required evidence"):
        execute_release(memory, session_id="fresh", root=tmp_path)


def test_timed_out_recheck_revokes_earlier_receipt_and_approval(tmp_path: Path):
    slow_flag = tmp_path / ".comeback" / "slow-checkpoint"
    checkpoint_code = (
        "from pathlib import Path; import time; "
        "time.sleep(5 if Path('.comeback/slow-checkpoint').exists() else 0)"
    )
    memory, owner = _supervised_memory(
        tmp_path,
        checkpoint_argv=[sys.executable, "-c", checkpoint_code],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    slow_flag.parent.mkdir(parents=True, exist_ok=True)
    slow_flag.write_text("slow\n", encoding="utf-8")

    with pytest.raises(MemoryIntegrityError, match="timed out"):
        execute_checkpoint(memory, session_id="fresh", root=tmp_path, timeout=1)

    run = memory.get_run("fresh")
    assert run["checkpoint_attempt"] is None
    assert run["checkpoint_receipt"] is None
    assert run.get("approval") is None
    assert memory.missing_requirements(run) == [
        "human_approval",
        "release_check_passed",
    ]


def test_checkpoint_attempt_nonce_rejects_overlap_and_stale_completion(tmp_path: Path):
    memory, _ = _supervised_memory(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    memory.begin_checkpoint_attempt("fresh", attempt_id="attempt-a", started_at=now)

    with pytest.raises(MemoryIntegrityError, match="another checkpoint attempt"):
        memory.begin_checkpoint_attempt("fresh", attempt_id="attempt-b", started_at=now)

    memory.finish_checkpoint_attempt(
        "fresh", attempt_id="attempt-a", reason="test_transition"
    )
    memory.begin_checkpoint_attempt("fresh", attempt_id="attempt-b", started_at=now)
    with pytest.raises(MemoryIntegrityError, match="stale attempt"):
        memory.record_checkpoint_receipt(
            "fresh",
            {},
            attempt_id="attempt-a",
        )
    assert memory.get_run("fresh")["checkpoint_attempt"]["attempt_id"] == "attempt-b"


def test_tampered_stored_approval_is_rejected_before_release(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    run = memory.get_run("fresh")
    run["approval"]["signature"] = "0x" + "00" * 65
    memory.client.set_entity(memory.RUN_CATEGORY, "fresh", run, status="open")

    with pytest.raises(MemoryIntegrityError, match="stored human approval"):
        execute_release(memory, session_id="fresh", root=tmp_path)
    assert not (tmp_path / "released.txt").exists()


def test_release_capability_is_one_shot(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    _, first_exit = execute_release(memory, session_id="fresh", root=tmp_path)
    assert first_exit == 0

    with pytest.raises(MemoryIntegrityError, match="not open"):
        execute_release(memory, session_id="fresh", root=tmp_path)


def test_earned_autonomous_run_still_uses_capability_and_records_outcome(
    tmp_path: Path,
):
    memory, owner = _supervised_memory(tmp_path)
    for index, session_id in enumerate(("fresh", "success-two", "success-three")):
        if index:
            run = memory.start_run(
                session_id=session_id,
                task_class="release",
                area="release_workflow",
                agent_family="Codex",
                model="test",
            )
        else:
            run = memory.get_run(session_id)
        execute_checkpoint(memory, session_id=session_id, root=tmp_path)
        if run["mode"] == "HUMAN_REQUIRED":
            _approve(memory, owner, session_id)
        execute_release(memory, session_id=session_id, root=tmp_path)

    autonomous = memory.start_run(
        session_id="earned-autonomous",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert autonomous["mode"] == "AUTONOMOUS"
    assert autonomous["required_evidence"] == []

    result, exit_code = execute_release(
        memory,
        session_id="earned-autonomous",
        root=tmp_path,
    )
    assert exit_code == 0
    assert result["outcome"] == "success"
    assert memory.get_run("earned-autonomous")["status"] == "completed"


def test_earned_autonomous_git_release_pins_current_commit_without_receipt(
    tmp_path: Path,
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    root = tmp_path / "work"
    memory, owner = _supervised_memory(
        root,
        release_argv=["git", "push", str(remote), "HEAD:refs/heads/main"],
    )
    for index, session_id in enumerate(("fresh", "git-two", "git-three")):
        if index:
            run = memory.start_run(
                session_id=session_id,
                task_class="release",
                area="release_workflow",
                agent_family="Codex",
                model="test",
            )
        else:
            run = memory.get_run(session_id)
        execute_checkpoint(memory, session_id=session_id, root=root)
        if run["mode"] == "HUMAN_REQUIRED":
            _approve(memory, owner, session_id)
        execute_release(memory, session_id=session_id, root=root)

    autonomous = memory.start_run(
        session_id="git-autonomous",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert autonomous["mode"] == "AUTONOMOUS"
    assert autonomous["checkpoint_receipt"] is None

    result, exit_code = execute_release(
        memory, session_id="git-autonomous", root=root
    )
    assert exit_code == 0
    assert result["outcome"] == "success"


def test_signed_git_release_rejects_mutable_remote_name(tmp_path: Path):
    with pytest.raises(MemoryIntegrityError, match="direct URL or absolute path"):
        _supervised_memory(
            tmp_path,
            require_clean_git=False,
            release_argv=["git", "push", "origin", "HEAD:refs/heads/main"],
        )


def test_direct_git_target_cannot_be_redirected_by_remote_mutation(tmp_path: Path):
    first_remote = tmp_path / "first.git"
    second_remote = tmp_path / "second.git"
    subprocess.run(["git", "init", "--bare", "-q", str(first_remote)], check=True)
    subprocess.run(["git", "init", "--bare", "-q", str(second_remote)], check=True)
    root = tmp_path / "work"
    memory, owner = _supervised_memory(
        root,
        require_clean_git=False,
        release_argv=[
            "git",
            "push",
            str(first_remote),
            "HEAD:refs/heads/main",
        ],
    )
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", str(first_remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "remote", "set-url", "origin", str(second_remote)],
        check=True,
    )
    execute_checkpoint(memory, session_id="fresh", root=root)
    _approve(memory, owner)
    _, exit_code = execute_release(memory, session_id="fresh", root=root)
    assert exit_code == 0
    first_pushed = subprocess.run(
        ["git", "--git-dir", str(first_remote), "rev-parse", "refs/heads/main"],
        capture_output=True,
        check=False,
    )
    second_pushed = subprocess.run(
        ["git", "--git-dir", str(second_remote), "rev-parse", "refs/heads/main"],
        capture_output=True,
        check=False,
    )
    assert first_pushed.returncode == 0
    assert second_pushed.returncode != 0


def test_git_release_pushes_checkpointed_commit_during_concurrent_head_change(
    tmp_path: Path, monkeypatch
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    root = tmp_path / "work"
    memory, owner = _supervised_memory(
        root,
        require_clean_git=False,
        release_argv=["git", "push", str(remote), "HEAD:refs/heads/main"],
    )
    execute_checkpoint(memory, session_id="fresh", root=root)
    approved_head = memory.get_run("fresh")["checkpoint_receipt"]["repository_head"]
    _approve(memory, owner)
    original_begin = memory.begin_release

    def begin_then_commit(session_id: str, *, state_fingerprint: str):
        result = original_begin(session_id, state_fingerprint=state_fingerprint)
        (root / "after-checkpoint.txt").write_text("new commit\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "after-checkpoint.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "concurrent commit"],
            check=True,
        )
        return result

    monkeypatch.setattr(memory, "begin_release", begin_then_commit)
    _, exit_code = execute_release(memory, session_id="fresh", root=root)
    current_head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert exit_code == 0
    assert current_head != approved_head
    assert remote_head == approved_head


def test_concurrent_runs_cannot_both_execute_for_one_lesson(tmp_path: Path):
    release_code = (
        "import time; from pathlib import Path; time.sleep(0.4); "
        "p=Path('release-count.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    )
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", release_code],
    )
    for session_id in ("fresh", "concurrent-two"):
        if session_id != "fresh":
            memory.start_run(
                session_id=session_id,
                task_class="release",
                area="release_workflow",
                agent_family="Codex",
                model="test",
            )
        execute_checkpoint(memory, session_id=session_id, root=tmp_path)
        _approve(memory, owner, session_id)

    def attempt(session_id: str):
        try:
            return execute_release(memory, session_id=session_id, root=tmp_path)
        except MemoryIntegrityError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("fresh", "concurrent-two")))

    successes = [result for result in results if isinstance(result, tuple)]
    refusals = [result for result in results if isinstance(result, MemoryIntegrityError)]
    assert len(successes) == 1
    assert len(refusals) == 1
    assert (tmp_path / "release-count.txt").read_text(encoding="utf-8") == "x"


def test_timeout_creates_unresolved_debt_and_forces_human_required(tmp_path: Path):
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", "import time; time.sleep(2)"],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)

    with pytest.raises(MemoryIntegrityError, match="outcome is unknown"):
        execute_release(memory, session_id="fresh", root=tmp_path, timeout=1)

    uncertain = memory.get_run("fresh")
    lesson = memory.matching_lessons("release", "release_workflow", "Codex")[0]
    next_run = memory.start_run(
        session_id="after-unknown",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert uncertain["status"] == "unknown"
    assert uncertain["outcome"] == "unknown"
    assert lesson["unresolved_release_count"] == 1
    assert lesson["current_mode"] == "HUMAN_REQUIRED"
    assert next_run["mode"] == "HUMAN_REQUIRED"


def test_interrupt_after_sibyl_begin_retains_unknown_recovery_lock(
    tmp_path: Path, monkeypatch
):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    original_begin = memory.begin_release

    def begin_then_interrupt(session_id: str, *, state_fingerprint: str):
        original_begin(session_id, state_fingerprint=state_fingerprint)
        raise KeyboardInterrupt()

    monkeypatch.setattr(memory, "begin_release", begin_then_interrupt)

    with pytest.raises(MemoryIntegrityError, match="outcome is unknown"):
        execute_release(memory, session_id="fresh", root=tmp_path)

    assert not (tmp_path / "released.txt").exists()
    assert memory.get_run("fresh")["status"] == "unknown"
    assert read_release_lock(tmp_path, memory.repo_id)["phase"] == "outcome_unknown_persisted"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_timeout_kills_descendant_that_ignores_sigterm(tmp_path: Path):
    marker = tmp_path / "late-child.txt"
    child_code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(2); "
        f"Path({str(marker)!r}).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(10)"
    )
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", parent_code],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)

    with pytest.raises(MemoryIntegrityError, match="outcome is unknown"):
        execute_release(memory, session_id="fresh", root=tmp_path, timeout=1)
    time.sleep(2.2)
    assert not marker.exists()
    assert read_release_lock(tmp_path, memory.repo_id)["phase"] == "outcome_unknown_persisted"


def test_stale_checkpoint_must_be_repeated_before_release(tmp_path: Path, monkeypatch):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    monkeypatch.setattr(
        memory_module, "_MAX_CHECKPOINT_AGE", timedelta(seconds=-1)
    )

    with pytest.raises(MemoryIntegrityError, match="older than 15 minutes"):
        execute_release(memory, session_id="fresh", root=tmp_path)
    assert not (tmp_path / "released.txt").exists()


def test_nonzero_release_is_unknown_until_owner_reconciles(tmp_path: Path):
    release_code = (
        "from pathlib import Path; Path('released.txt').write_text('partial'); "
        "raise SystemExit(7)"
    )
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", release_code],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)

    result, exit_code = execute_release(memory, session_id="fresh", root=tmp_path)

    assert exit_code == 7
    assert result["decision"] == "release_outcome_unknown"
    assert memory.get_run("fresh")["status"] == "unknown"
    assert read_release_lock(tmp_path, memory.repo_id)["session_id"] == "fresh"

    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(
            text=reconciliation_message(run, "released", resolved_at)
        ),
        private_key=owner.key,
    ).signature.hex()
    reconciled = reconcile_release(
        memory,
        session_id="fresh",
        root=tmp_path,
        resolution="released",
        resolved_at=resolved_at,
        signature=signature,
    )

    assert reconciled["status"] == "completed"
    assert reconciled["outcome"] == "success"
    assert reconciled["release_lock_cleared"] is True
    assert read_release_lock(tmp_path, memory.repo_id) is None
    lesson = memory.all_lessons()[0]
    assert lesson["unresolved_release_count"] == 0
    assert lesson["applied_release_outcomes"]["fresh"] == "success"
    assert lesson["success_count"] == 1
    assert lesson["probation_success_count"] == 1
    assert lesson["current_mode"] == "CHECKPOINTED"


def test_dead_open_reservation_can_be_closed_without_penalizing_policy(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    before = memory.all_lessons()[0]
    lock, _ = _release_lock(tmp_path, memory.repo_id, "fresh")
    lock["wrapper_process_id"] = 99_999_999
    release_lock_path(tmp_path, memory.repo_id).write_text(
        json.dumps(lock), encoding="utf-8"
    )
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(
            text=reconciliation_message(run, "not_released", resolved_at)
        ),
        private_key=owner.key,
    ).signature.hex()

    result = reconcile_release(
        memory,
        session_id="fresh",
        root=tmp_path,
        resolution="not_released",
        resolved_at=resolved_at,
        signature=signature,
    )

    after = memory.all_lessons()[0]
    assert result["status"] == "failed"
    assert result["outcome_reason"] == "reserved_release_never_started"
    assert result["release_lock_cleared"] is True
    assert after["failure_count"] == before["failure_count"]
    assert after["success_count"] == before["success_count"]


def test_executing_reserved_lock_without_child_identity_fails_closed(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    lock, _ = _release_lock(tmp_path, memory.repo_id, "fresh")
    lock["wrapper_process_id"] = 99_999_999
    release_lock_path(tmp_path, memory.repo_id).write_text(
        json.dumps(lock), encoding="utf-8"
    )
    run = memory.get_run("fresh")
    memory.begin_release(
        "fresh", state_fingerprint=run["checkpoint_receipt"]["state_fingerprint"]
    )
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(
            text=reconciliation_message(run, "not_released", resolved_at)
        ),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="child identity"):
        reconcile_release(
            memory,
            session_id="fresh",
            root=tmp_path,
            resolution="not_released",
            resolved_at=resolved_at,
            signature=signature,
        )
    assert read_release_lock(tmp_path, memory.repo_id)["phase"] == "reserved"


def test_unopened_start_barrier_cannot_be_reconciled_as_released(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    lock, _ = _release_lock(tmp_path, memory.repo_id, "fresh")
    lock.update(
        {
            "wrapper_process_id": 99_999_991,
            "wrapper_process_birth_token": "dead-wrapper",
            "child_process_id": 99_999_992,
            "child_process_birth_token": "dead-runner",
            "process_group_id": 99_999_992,
            "child_started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "barrier_ready",
        }
    )
    release_lock_path(tmp_path, memory.repo_id).write_text(
        json.dumps(lock), encoding="utf-8"
    )
    run = memory.get_run("fresh")
    memory.begin_release(
        "fresh", state_fingerprint=run["checkpoint_receipt"]["state_fingerprint"]
    )
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(text=reconciliation_message(run, "released", resolved_at)),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="only be reconciled as not_released"):
        reconcile_release(
            memory,
            session_id="fresh",
            root=tmp_path,
            resolution="released",
            resolved_at=resolved_at,
            signature=signature,
        )


def test_live_child_or_result_persisting_wrapper_blocks_reconciliation(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    run = memory.get_run("fresh")
    memory.begin_release(
        "fresh", state_fingerprint=run["checkpoint_receipt"]["state_fingerprint"]
    )
    lock, _ = _release_lock(tmp_path, memory.repo_id, "fresh")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        start_new_session=os.name != "nt",
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    try:
        lock.update(
            {
                "wrapper_process_id": 99_999_999,
                "child_process_id": process.pid,
                "child_process_birth_token": _required_process_birth_token(process.pid),
                "process_group_id": process.pid,
                "child_started_at": datetime.now(timezone.utc).isoformat(),
                "phase": "child_started",
            }
        )
        release_lock_path(tmp_path, memory.repo_id).write_text(
            json.dumps(lock), encoding="utf-8"
        )
        run = memory.get_run("fresh")
        resolved_at = datetime.now(timezone.utc).isoformat()
        signature = Account.sign_message(
            encode_defunct(
                text=reconciliation_message(run, "not_released", resolved_at)
            ),
            private_key=owner.key,
        ).signature.hex()
        with pytest.raises(MemoryIntegrityError, match="recorded process is stopped"):
            reconcile_release(
                memory,
                session_id="fresh",
                root=tmp_path,
                resolution="not_released",
                resolved_at=resolved_at,
                signature=signature,
            )

        lock["phase"] = "child_exited"
        lock["wrapper_process_id"] = os.getpid()
        lock["child_process_id"] = 99_999_999
        release_lock_path(tmp_path, memory.repo_id).write_text(
            json.dumps(lock), encoding="utf-8"
        )
        with pytest.raises(MemoryIntegrityError, match="recorded process is stopped"):
            reconcile_release(
                memory,
                session_id="fresh",
                root=tmp_path,
                resolution="not_released",
                resolved_at=resolved_at,
                signature=signature,
            )
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_reconciliation_rejects_wrong_signer_and_retains_lock(tmp_path: Path):
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", "raise SystemExit(9)"],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    execute_release(memory, session_id="fresh", root=tmp_path)
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    wrong = Account.create()
    signature = Account.sign_message(
        encode_defunct(
            text=reconciliation_message(run, "not_released", resolved_at)
        ),
        private_key=wrong.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="authorized closer"):
        reconcile_release(
            memory,
            session_id="fresh",
            root=tmp_path,
            resolution="not_released",
            resolved_at=resolved_at,
            signature=signature,
        )

    assert read_release_lock(tmp_path, memory.repo_id)["session_id"] == "fresh"


def test_old_run_cannot_clear_another_sessions_release_lock(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    execute_release(memory, session_id="fresh", root=tmp_path)
    _release_lock(tmp_path, memory.repo_id, "active-other")
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(text=reconciliation_message(run, "released", resolved_at)),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="another release owns"):
        reconcile_release(
            memory,
            session_id="fresh",
            root=tmp_path,
            resolution="released",
            resolved_at=resolved_at,
            signature=signature,
        )
    assert read_release_lock(tmp_path, memory.repo_id)["session_id"] == "active-other"


def test_completed_release_without_retained_lock_cannot_be_reconciled(tmp_path: Path):
    memory, owner = _supervised_memory(tmp_path)
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    execute_release(memory, session_id="fresh", root=tmp_path)
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(text=reconciliation_message(run, "released", resolved_at)),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="matching retained lock"):
        reconcile_release(
            memory,
            session_id="fresh",
            root=tmp_path,
            resolution="released",
            resolved_at=resolved_at,
            signature=signature,
        )


def test_reconciliation_rejects_future_and_predating_timestamps(tmp_path: Path):
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", "raise SystemExit(7)"],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    execute_release(memory, session_id="fresh", root=tmp_path)
    run = memory.get_run("fresh")

    for invalid_at, expected in (
        (
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "timestamp",
        ),
        (
            (datetime.fromisoformat(run["release_started_at"]) - timedelta(seconds=1)).isoformat(),
            "chronology",
        ),
    ):
        signature = Account.sign_message(
            encode_defunct(
                text=reconciliation_message(run, "not_released", invalid_at)
            ),
            private_key=owner.key,
        ).signature.hex()
        with pytest.raises(MemoryIntegrityError, match=expected):
            reconcile_release(
                memory,
                session_id="fresh",
                root=tmp_path,
                resolution="not_released",
                resolved_at=invalid_at,
                signature=signature,
            )


def test_stored_reconciliation_signature_is_self_validating(tmp_path: Path):
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", "raise SystemExit(7)"],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    execute_release(memory, session_id="fresh", root=tmp_path)
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(
            text=reconciliation_message(run, "not_released", resolved_at)
        ),
        private_key=owner.key,
    ).signature.hex()
    reconcile_release(
        memory,
        session_id="fresh",
        root=tmp_path,
        resolution="not_released",
        resolved_at=resolved_at,
        signature=signature,
    )
    stored = memory.client.get_entity(memory.RUN_CATEGORY, "fresh")["body"]
    stored["reconciliation"]["signature"] = "0x" + "00" * 65
    memory.client.set_entity(memory.RUN_CATEGORY, "fresh", stored, status="failed")

    with pytest.raises(MemoryIntegrityError, match="reconciliation signature"):
        memory.get_run("fresh")


def test_reconciliation_journal_outage_does_not_strand_release_lock(
    tmp_path: Path, monkeypatch
):
    memory, owner = _supervised_memory(
        tmp_path,
        release_argv=[sys.executable, "-c", "raise SystemExit(7)"],
    )
    execute_checkpoint(memory, session_id="fresh", root=tmp_path)
    _approve(memory, owner)
    execute_release(memory, session_id="fresh", root=tmp_path)
    run = memory.get_run("fresh")
    resolved_at = datetime.now(timezone.utc).isoformat()
    signature = Account.sign_message(
        encode_defunct(
            text=reconciliation_message(run, "not_released", resolved_at)
        ),
        private_key=owner.key,
    ).signature.hex()

    def fail_journal(**_kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(memory.client, "write_event", fail_journal)
    result = reconcile_release(
        memory,
        session_id="fresh",
        root=tmp_path,
        resolution="not_released",
        resolved_at=resolved_at,
        signature=signature,
    )

    assert result["status"] == "failed"
    assert result["release_lock_cleared"] is True
    assert read_release_lock(tmp_path, memory.repo_id) is None
