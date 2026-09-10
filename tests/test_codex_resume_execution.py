"""Opt-in real CLI restart regression, not a lifecycle-hook or cold-user test."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_execution import _approve, _supervised_memory


@pytest.mark.skipif(
    os.environ.get("COMEBACK_TEST_AUTHENTICATED_CODEX") != "1",
    reason="opt-in: requires authenticated Codex and consumes two model turns",
)
def test_real_codex_resume_preserves_approved_capability(tmp_path: Path):
    codex = shutil.which("codex")
    assert codex, "authenticated Codex CLI is required"
    memory, owner = _supervised_memory(tmp_path)
    helper = tmp_path / ".comeback" / "context_probe.py"
    helper.write_text(
        "import hashlib, json, os, sys\n"
        "from pathlib import Path\n"
        "from comeback.execution import _capability_environment, execute_checkpoint, execute_release\n"
        "from comeback.memory import InterventionMemory\n"
        "root = Path(__file__).resolve().parent.parent\n"
        "operation = sys.argv[1]\n"
        "assert operation in ('checkpoint', 'release')\n"
        "raw = hashlib.sha256(os.environ.get('PATH', '').encode()).hexdigest()\n"
        "effective = hashlib.sha256(_capability_environment().get('PATH', '').encode()).hexdigest()\n"
        "with InterventionMemory(root / '.comeback' / 'memory.db', 'repo-a') as memory:\n"
        "    function = execute_checkpoint if operation == 'checkpoint' else execute_release\n"
        "    result, code = function(memory, session_id='fresh', root=root)\n"
        "report = dict(result=result, code=code, pid=os.getpid(), raw_path_sha256=raw, effective_path_sha256=effective)\n"
        "(root / '.comeback' / (operation + '-observed.json')).write_text(json.dumps(report))\n"
        "print(json.dumps(report))\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )

    def run_agent(operation: str, session_id: str | None = None):
        argv = [sys.executable, str(helper), operation]
        prompt = (
            "This is an isolated capability regression using a generated test owner, "
            "not production authority. Run exactly this argv once in the native shell: "
            + json.dumps(argv)
            + ". Do not modify environment, edit files, run any other commands, sign, "
            "change permissions, or rerun after failure. Report its output and stop."
        )
        command = [codex, "exec", "--sandbox", "workspace-write", "--json"]
        if session_id:
            command.extend(["resume", session_id])
        command.append(prompt)
        completed = subprocess.run(
            command, cwd=tmp_path, capture_output=True, text=True, timeout=180,
        )
        (tmp_path / ".comeback" / (operation + "-codex.jsonl")).write_text(
            completed.stdout + completed.stderr, encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr[-3000:]
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        thread = next(event["thread_id"] for event in events if event.get("type") == "thread.started")
        observed = json.loads((tmp_path / ".comeback" / (operation + "-observed.json")).read_text())
        assert observed["code"] == 0
        return thread, observed

    with memory:
        thread, checkpoint = run_agent("checkpoint")
        assert checkpoint["result"]["decision"] == "checkpoint_recorded"
        assert checkpoint["result"]["remaining"] == ["human_approval"]
        receipt = memory.get_run("fresh")["checkpoint_receipt"]["digest"]
        _approve(memory, owner)
        approval = memory.get_run("fresh")["approval"]
        resumed_thread, release = run_agent("release", thread)
        assert resumed_thread == thread
        assert checkpoint["pid"] != release["pid"]
        assert checkpoint["raw_path_sha256"] != release["raw_path_sha256"], "probe did not reproduce PATH churn"
        assert checkpoint["effective_path_sha256"] == release["effective_path_sha256"]
        assert release["result"]["outcome"] == "success"
        assert (tmp_path / "released.txt").read_text() == "ok"
        final = memory.get_run("fresh")
        assert final["checkpoint_receipt"]["digest"] == receipt
        assert final["approval"] == approval
        print(json.dumps({
            "gate": "PASS", "codex_thread": thread,
            "checkpoint": checkpoint, "release": release,
            "receipt_unchanged": True, "approval_unchanged": True,
            "limits": "generated test owner; explicit capability calls, not lifecycle hook validation or cold-user evidence",
        }))
