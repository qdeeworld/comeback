from datetime import datetime, timezone

from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.identity import repository_identity
from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.signing import intervention_message


def record_with_source(memory: InterventionMemory, record: dict) -> dict:
    fields = record["signed_fields"]
    memory.start_run(
        session_id=fields["source_session_id"],
        task_class=fields["task_class"],
        area=fields["area"],
        agent_family=fields["agent_family"],
        model="test-source",
    )
    return memory.record_intervention(record)


def test_all_supported_scope_carries_codex_intervention_into_claude(tmp_path: Path):
    db = tmp_path / "memory.db"
    owner = Account.create()
    _, repo_id = repository_identity(tmp_path)
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "agent_scope": "all_supported",
        "severity": "release_blocker",
        "action_schema": 2,
        "checkpoint_spec": {"argv": ["pnpm", "run", "release:check"], "timeout_seconds": 600},
        "release_spec": {"argv": ["git", "push", "https://example.test/org/repo.git", "HEAD:refs/heads/main"], "timeout_seconds": 600},
        "state_policy": {"bind_head": True, "require_clean_git": True},
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "codex-correction",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)), private_key=owner.key
    ).signature.hex()
    memory = InterventionMemory(db, repo_id)
    record_with_source(memory,
        {
            "signed_fields": signed_fields,
            "intervention_signature": signature,
            "incident_summary": "Codex skipped the release check.",
        }
    )

    run = memory.start_run(
        session_id="fresh-claude",
        task_class="release",
        area="release_workflow",
        agent_family="ClaudeCode",
        model="claude",
    )

    assert run["mode"] == "HUMAN_REQUIRED"
    assert run["lesson_ids"] == ["release-release_workflow-codex"]
    assert run["checkpoint_spec"]["argv"] == ["pnpm", "run", "release:check"]


def test_same_agent_scope_does_not_cross_agent_boundary(tmp_path: Path):
    db = tmp_path / "memory.db"
    owner = Account.create()
    _, repo_id = repository_identity(tmp_path)
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "agent_scope": "same_agent",
        "severity": "release_blocker",
        "action_schema": 2,
        "checkpoint_spec": {"argv": ["pnpm", "run", "release:check"], "timeout_seconds": 600},
        "release_spec": {"argv": ["git", "push", "https://example.test/org/repo.git", "HEAD:refs/heads/main"], "timeout_seconds": 600},
        "state_policy": {"bind_head": True, "require_clean_git": True},
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "codex-correction",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)), private_key=owner.key
    ).signature.hex()
    memory = InterventionMemory(db, repo_id)
    record_with_source(memory,
        {
            "signed_fields": signed_fields,
            "intervention_signature": signature,
            "incident_summary": "Codex skipped the release check.",
        }
    )

    run = memory.start_run(
        session_id="fresh-claude",
        task_class="release",
        area="release_workflow",
        agent_family="ClaudeCode",
        model="claude",
    )

    assert run["mode"] == "AUTONOMOUS"
    assert run["lesson_ids"] == []


def test_legacy_scope_cannot_be_widened_without_a_new_signature(tmp_path: Path):
    db = tmp_path / "memory.db"
    owner = Account.create()
    memory = InterventionMemory(db, "repo-a")
    lesson = record_with_source(
        memory, signed_record("repo-a", owner.key, owner.address)
    )
    lesson["agent_scope"] = "all_supported"
    memory.client.set_entity(memory.LESSON_CATEGORY, lesson["lesson_id"], lesson, status="active")

    with pytest.raises(MemoryIntegrityError, match="differs from its signed scope"):
        memory.matching_lessons("release", "release_workflow", "ClaudeCode")


def signed_record(repo_id: str, private_key: str, address: str) -> dict:
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "severity": "release_blocker",
        "action_schema": 2,
        "checkpoint_spec": {"argv": ["python", "scripts/release_check.py"], "timeout_seconds": 600},
        "release_spec": {"argv": ["git", "push", "https://example.test/org/repo.git", "HEAD:refs/heads/main"], "timeout_seconds": 600},
        "state_policy": {"bind_head": True, "require_clean_git": True},
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": address.lower(),
        "source_session_id": "session-one",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)), private_key=private_key
    ).signature.hex()
    return {
        "signed_fields": signed_fields,
        "intervention_signature": signature,
        "incident_summary": "Agent skipped the release check.",
    }


def test_signed_intervention_changes_fresh_run(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    run = memory.start_run(
        session_id="fresh-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert run["mode"] == "HUMAN_REQUIRED"
    assert memory.missing_requirements(run) == ["human_approval", "release_check_passed"]


def test_intervention_from_wrong_repo_is_rejected(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-b")
    with pytest.raises(MemoryIntegrityError, match="another repository"):
        memory.record_intervention(signed_record("repo-a", owner.key.hex(), owner.address))


def test_existing_lesson_closer_cannot_be_replaced(tmp_path):
    owner = Account.create()
    attacker = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )

    with pytest.raises(MemoryIntegrityError, match="already anchored"):
        memory.record_intervention(signed_record("repo-a", attacker.key.hex(), attacker.address))


def test_exact_intervention_retry_is_idempotent(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record = signed_record("repo-a", owner.key.hex(), owner.address)
    first = record_with_source(memory, record)
    retried = memory.record_intervention(record)
    assert retried["failure_count"] == first["failure_count"] == 1
    assert retried["revision"] == first["revision"] == 1


def test_one_source_session_cannot_be_counted_as_two_interventions(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    first_record = signed_record("repo-a", owner.key.hex(), owner.address)
    first = record_with_source(memory, first_record)
    changed = signed_record("repo-a", owner.key.hex(), owner.address)
    changed["signed_fields"]["incident_at"] = datetime.now(timezone.utc).isoformat()
    changed["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(changed["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="already has a different intervention"):
        memory.record_intervention(changed)
    assert memory.all_lessons()[0]["intervention_count"] == 1
    assert memory.all_lessons()[0]["failure_count"] == first["failure_count"]


def test_new_intervention_immediately_resets_earned_autonomy(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    first = record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    first["success_count"] = 10
    first["probation_success_count"] = 10
    first["current_mode"] = "AUTONOMOUS"
    first["revision"] += 1
    memory.client.set_entity(
        memory.LESSON_CATEGORY, first["lesson_id"], first, status="active"
    )
    memory.start_run(
        session_id="session-two",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test-source",
    )
    second = signed_record("repo-a", owner.key.hex(), owner.address)
    second["signed_fields"]["source_session_id"] = "session-two"
    second["signed_fields"]["incident_at"] = datetime.now(timezone.utc).isoformat()
    second["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(second["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()

    updated = memory.record_intervention(second)

    assert updated["success_count"] == 10
    assert updated["failure_count"] == 2
    assert updated["intervention_count"] == 2
    assert updated["probation_success_count"] == 0
    assert updated["current_mode"] == "HUMAN_REQUIRED"
    incidents = memory.client.list_entities(memory.INCIDENT_CATEGORY, limit=10)
    assert len(incidents) == 2


def test_cross_agent_lesson_cannot_install_a_second_repository_closer(tmp_path):
    owner = Account.create()
    attacker = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    memory.start_run(
        session_id="claude-source",
        task_class="release",
        area="release_workflow",
        agent_family="ClaudeCode",
        model="test-source",
    )
    second = signed_record("repo-a", attacker.key.hex(), attacker.address)
    second["signed_fields"].update(
        {
            "lesson_id": "release-release_workflow-claudecode",
            "agent_family": "ClaudeCode",
            "source_session_id": "claude-source",
        }
    )
    second["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(second["signed_fields"])),
        private_key=attacker.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="anchored for this repository"):
        memory.record_intervention(second)


def test_intervention_must_match_its_exact_sibyl_source_run(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record = signed_record("repo-a", owner.key.hex(), owner.address)
    memory.start_run(
        session_id="session-one",
        task_class="low_risk",
        area="general",
        agent_family="Codex",
        model="test-source",
    )

    with pytest.raises(MemoryIntegrityError, match="does not match its Sibyl source run"):
        memory.record_intervention(record)


def test_arbitrary_lesson_id_cannot_poison_or_evict_supported_memory(tmp_path):
    owner = Account.create()
    record = signed_record("repo-a", owner.key.hex(), owner.address)
    record["signed_fields"]["lesson_id"] = "junk-lesson"
    record["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(record["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="deterministic supported ID"):
        InterventionMemory(tmp_path / "memory.db", "repo-a").record_intervention(record)


def test_open_run_survives_followup_prompt(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    first = memory.start_run(
        session_id="same-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    resumed = memory.start_run(
        session_id="same-session",
        task_class="low_risk",
        area="general",
        agent_family="Codex",
        model="test",
    )
    assert resumed["task_class"] == "release"
    assert resumed["satisfied_evidence"] == []


def test_terminal_run_is_preserved_and_requires_a_fresh_session(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    run = memory.start_run(
        session_id="completed-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    run["status"] = "completed"
    run["outcome"] = "success"
    memory.client.set_entity(
        memory.RUN_CATEGORY, "completed-session", run, status="completed"
    )

    resumed = memory.start_run(
        session_id="completed-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    assert resumed["status"] == "completed"
    assert resumed["outcome"] == "success"


@pytest.mark.parametrize(
    "shell",
    ["bash", "sh", "zsh", "cmd.exe", "powershell.exe", "pwsh"],
)
def test_signed_action_specs_reject_shell_interpreters(tmp_path, shell):
    owner = Account.create()
    record = signed_record("repo-a", owner.key.hex(), owner.address)
    record["signed_fields"]["release_spec"]["argv"] = [shell, "-c", "git push"]
    record["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(record["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="shell interpreter"):
        InterventionMemory(tmp_path / "memory.db", "repo-a").record_intervention(record)


@pytest.mark.parametrize("batch", ["deploy.cmd", "C:/tools/release.BAT"])
def test_signed_action_specs_reject_windows_batch_files(tmp_path, batch):
    owner = Account.create()
    record = signed_record("repo-a", owner.key.hex(), owner.address)
    record["signed_fields"]["checkpoint_spec"]["argv"] = [batch, "--verify"]
    record["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(record["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(MemoryIntegrityError, match="Windows batch file"):
        InterventionMemory(tmp_path / "memory.db", "repo-a").record_intervention(record)


def test_existing_open_run_is_reverified_before_reuse(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    run = memory.start_run(
        session_id="same-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    run["mode"] = "AUTONOMOUS"
    memory.client.set_entity(memory.RUN_CATEGORY, "same-session", run, status="open")

    with pytest.raises(MemoryIntegrityError, match="mode differs"):
        memory.start_run(
            session_id="same-session",
            task_class="release",
            area="release_workflow",
            agent_family="Codex",
            model="test",
        )


def test_corrupted_run_status_is_rejected(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    run = memory.start_run(
        session_id="corrupt-status",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    run["status"] = "garbage"
    memory.client.set_entity(
        memory.RUN_CATEGORY, "corrupt-status", run, status="garbage"
    )

    with pytest.raises(MemoryIntegrityError, match="run status"):
        memory.get_run("corrupt-status")


def test_open_run_cannot_claim_a_release_outcome_without_execution(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    record_with_source(
        memory, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    memory.start_run(
        session_id="unexecuted",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )

    with pytest.raises(MemoryIntegrityError, match="executing one-shot capability"):
        memory.record_release_outcome("unexecuted", success=True)
