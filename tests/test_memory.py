from datetime import datetime, timezone

from copy import deepcopy
import hashlib
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.base_trust import BaseTrustError
from comeback.identity import BaseTrustConfig, repository_identity
from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.signing import intervention_message


class _BaseClient:
    def __init__(self, anchor_key: str, *, fail: bool = False) -> None:
        self.expected_anchor_key = anchor_key
        self.fail = fail
        self.claim_calls = 0
        self.active_calls = 0

    def anchor_key(self, **_fields) -> str:
        return self.expected_anchor_key

    def verify_claim(self, **_fields):
        self.claim_calls += 1
        if self.fail:
            raise BaseTrustError("Base endpoint unavailable")
        return {"status": "claimed"}

    def verify_active(self, **_fields):
        self.active_calls += 1
        if self.fail:
            raise BaseTrustError("Base endpoint unavailable")
        return {"status": "active"}


def _base_config(owner: str, *, status: str, intervention_id: str | None = None):
    return BaseTrustConfig(
        required=True,
        chain_id=84532,
        registry_address="0x" + "11" * 20,
        runtime_code_hash="0x" + "22" * 32,
        owner_address=owner.lower(),
        nonce="0x" + "33" * 32,
        anchor_key="0x" + "44" * 32,
        claim_tx_hash="0x" + "55" * 32,
        claim_block_number=100,
        status=status,
        initial_intervention_id=intervention_id,
        activation_tx_hash=("0x" + "66" * 32) if status == "active" else None,
        activation_block_number=101 if status == "active" else None,
    )


def test_context_manager_closes_sibyl_storage(tmp_path: Path) -> None:
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    close = Mock(wraps=memory.client.storage.close)
    memory.client.storage.close = close

    with memory as selected:
        assert selected is memory
        selected.list_runs()

    close.assert_called_once_with()


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


def test_claimed_base_owner_controls_the_first_intervention(tmp_path):
    owner = Account.create()
    attacker = Account.create()
    trust = _base_config(owner.address, status="claimed")
    client = _BaseClient(trust.anchor_key)
    memory = InterventionMemory(
        tmp_path / "memory.db",
        "repo-a",
        base_trust=trust,
        base_client=client,
    )
    attacker_record = signed_record("repo-a", attacker.key.hex(), attacker.address)
    memory.start_run(
        session_id="session-one",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )

    with pytest.raises(MemoryIntegrityError, match="Base repository owner"):
        memory.record_intervention(attacker_record)

    lesson = memory.record_intervention(
        signed_record("repo-a", owner.key.hex(), owner.address)
    )
    assert lesson["authorized_closer"] == owner.address.lower()
    assert client.claim_calls == 1


def test_active_base_anchor_requires_its_exact_sibyl_intervention(tmp_path):
    owner = Account.create()
    database = tmp_path / "memory.db"
    claimed = _base_config(owner.address, status="claimed")
    with InterventionMemory(
        database,
        "repo-a",
        base_trust=claimed,
        base_client=_BaseClient(claimed.anchor_key),
    ) as memory:
        lesson = record_with_source(
            memory, signed_record("repo-a", owner.key.hex(), owner.address)
        )
    initial_id = lesson["applied_intervention_ids"][0]
    active = _base_config(
        owner.address,
        status="active",
        intervention_id=initial_id,
    )
    client = _BaseClient(active.anchor_key)
    with InterventionMemory(
        database,
        "repo-a",
        base_trust=active,
        base_client=client,
    ) as memory:
        run = memory.start_run(
            session_id="fresh-base-session",
            task_class="release",
            area="release_workflow",
            agent_family="Codex",
            model="test",
        )

    assert run["mode"] == "HUMAN_REQUIRED"
    assert run["lesson_ids"] == ["release-release_workflow-codex"]
    assert client.active_calls == 1


def test_base_intervention_id_commits_the_complete_signed_policy(tmp_path):
    owner = Account.create()
    claimed = _base_config(owner.address, status="claimed")
    first_record = signed_record("repo-a", owner.key.hex(), owner.address)
    substituted_record = deepcopy(first_record)
    substituted_fields = substituted_record["signed_fields"]
    substituted_fields["checkpoint_spec"]["argv"] = [
        "python",
        "scripts/substituted_check.py",
    ]
    substituted_record["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(substituted_fields)),
        private_key=owner.key,
    ).signature.hex()

    with InterventionMemory(
        tmp_path / "anchored.db",
        "repo-a",
        base_trust=claimed,
        base_client=_BaseClient(claimed.anchor_key),
    ) as memory:
        anchored = record_with_source(memory, first_record)
    anchored_id = anchored["applied_intervention_ids"][0]

    with InterventionMemory(
        tmp_path / "substituted.db",
        "repo-a",
        base_trust=claimed,
        base_client=_BaseClient(claimed.anchor_key),
    ) as memory:
        substituted = record_with_source(memory, substituted_record)
    substituted_id = substituted["applied_intervention_ids"][0]

    assert substituted_id != anchored_id

    active = _base_config(
        owner.address,
        status="active",
        intervention_id=anchored_id,
    )
    with InterventionMemory(
        tmp_path / "substituted.db",
        "repo-a",
        base_trust=active,
        base_client=_BaseClient(active.anchor_key),
    ) as memory:
        with pytest.raises(MemoryIntegrityError, match="activated Sibyl intervention"):
            memory.start_run(
                session_id="fresh-substitution-check",
                task_class="release",
                area="release_workflow",
                agent_family="Codex",
                model="test",
            )


def test_active_base_anchor_fails_closed_when_sibyl_store_is_deleted(tmp_path):
    owner = Account.create()
    active = _base_config(
        owner.address,
        status="active",
        intervention_id="77" * 32,
    )
    memory = InterventionMemory(
        tmp_path / "empty-memory.db",
        "repo-a",
        base_trust=active,
        base_client=_BaseClient(active.anchor_key),
    )

    with pytest.raises(MemoryIntegrityError, match="activated Sibyl intervention.*missing"):
        memory.start_run(
            session_id="fresh-after-deletion",
            task_class="release",
            area="release_workflow",
            agent_family="Codex",
            model="test",
        )


@pytest.mark.parametrize("mutation", ["delete", "substitute"])
def test_active_base_anchor_validates_the_incident_entity_not_only_projection(
    tmp_path,
    mutation,
):
    owner = Account.create()
    claimed = _base_config(owner.address, status="claimed")
    database = tmp_path / "memory.db"
    with InterventionMemory(
        database,
        "repo-a",
        base_trust=claimed,
        base_client=_BaseClient(claimed.anchor_key),
    ) as memory:
        record = signed_record("repo-a", owner.key.hex(), owner.address)
        lesson = record_with_source(memory, record)
        anchored_id = lesson["applied_intervention_ids"][0]
        if mutation == "delete":
            assert memory.client.delete_entity(memory.INCIDENT_CATEGORY, anchored_id)
        else:
            incident = memory.client.get_entity(
                memory.INCIDENT_CATEGORY,
                anchored_id,
            )["body"]
            incident["signed_fields"]["checkpoint_spec"]["argv"] = [
                "python",
                "scripts/substituted_check.py",
            ]
            memory.client.set_entity(
                memory.INCIDENT_CATEGORY,
                anchored_id,
                incident,
                status="recorded",
            )

    active = _base_config(
        owner.address,
        status="active",
        intervention_id=anchored_id,
    )
    with InterventionMemory(
        database,
        "repo-a",
        base_trust=active,
        base_client=_BaseClient(active.anchor_key),
    ) as memory:
        with pytest.raises(MemoryIntegrityError, match="missing or invalid"):
            memory.start_run(
                session_id=f"fresh-{mutation}",
                task_class="release",
                area="release_workflow",
                agent_family="Codex",
                model="test",
            )


def test_base_outage_blocks_release_but_not_low_risk_work(tmp_path):
    owner = Account.create()
    active = _base_config(
        owner.address,
        status="active",
        intervention_id="77" * 32,
    )
    memory = InterventionMemory(
        tmp_path / "memory.db",
        "repo-a",
        base_trust=active,
        base_client=_BaseClient(active.anchor_key, fail=True),
    )

    low_risk = memory.start_run(
        session_id="ordinary-edit",
        task_class="low_risk",
        area="general",
        agent_family="Codex",
        model="test",
    )
    assert low_risk["mode"] == "AUTONOMOUS"

    with pytest.raises(MemoryIntegrityError, match="Base trust verification failed"):
        memory.start_run(
            session_id="release-during-outage",
            task_class="release",
            area="release_workflow",
            agent_family="Codex",
            model="test",
        )


def test_committed_base_anchor_key_must_match_derived_key(tmp_path):
    owner = Account.create()
    active = _base_config(
        owner.address,
        status="active",
        intervention_id="77" * 32,
    )
    memory = InterventionMemory(
        tmp_path / "memory.db",
        "repo-a",
        base_trust=active,
        base_client=_BaseClient("0x" + "99" * 32),
    )

    with pytest.raises(MemoryIntegrityError, match="anchor key does not match"):
        memory.start_run(
            session_id="release-with-corrupt-anchor",
            task_class="release",
            area="release_workflow",
            agent_family="Codex",
            model="test",
        )


def test_slow_base_verification_does_not_hold_the_sibyl_mutation_lock(tmp_path):
    owner = Account.create()
    database = tmp_path / "memory.db"
    plain = InterventionMemory(database, "repo-a")
    lesson = record_with_source(
        plain, signed_record("repo-a", owner.key.hex(), owner.address)
    )
    plain.close()
    active = _base_config(
        owner.address,
        status="active",
        intervention_id=lesson["applied_intervention_ids"][0],
    )
    entered = threading.Event()
    proceed = threading.Event()

    class SlowBase(_BaseClient):
        def verify_active(self, **_fields):
            entered.set()
            assert proceed.wait(timeout=2)
            return {"status": "active"}

    release_memory = InterventionMemory(
        database,
        "repo-a",
        base_trust=active,
        base_client=SlowBase(active.anchor_key),
    )
    ordinary_memory = InterventionMemory(
        database,
        "repo-a",
        base_trust=active,
        base_client=_BaseClient(active.anchor_key, fail=True),
    )
    result: dict[str, object] = {}

    def open_release() -> None:
        result["release"] = release_memory.start_run(
            session_id="slow-release",
            task_class="release",
            area="release_workflow",
            agent_family="Codex",
            model="test",
        )

    thread = threading.Thread(target=open_release)
    thread.start()
    assert entered.wait(timeout=1)
    ordinary = ordinary_memory.start_run(
        session_id="concurrent-readme",
        task_class="low_risk",
        area="general",
        agent_family="Codex",
        model="test",
    )
    proceed.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert ordinary["mode"] == "AUTONOMOUS"
    assert result["release"]["mode"] == "HUMAN_REQUIRED"


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


def test_legacy_optional_scope_id_commits_exact_signed_payload_and_allows_history(
    tmp_path,
):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    first_record = signed_record("repo-a", owner.key.hex(), owner.address)
    assert "agent_scope" not in first_record["signed_fields"]
    first = record_with_source(memory, first_record)
    expected_first_id = hashlib.sha256(
        intervention_message(first_record["signed_fields"]).encode("utf-8")
    ).hexdigest()
    assert first["applied_intervention_ids"] == [expected_first_id]

    second_record = deepcopy(first_record)
    second_record["signed_fields"]["source_session_id"] = "legacy-session-two"
    second_record["signed_fields"]["incident_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    second_record["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(second_record["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()
    second = record_with_source(memory, second_record)

    assert second["intervention_count"] == 2
    assert second["applied_intervention_ids"][0] == expected_first_id


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


def test_historical_source_session_cannot_be_reused_after_a_later_intervention(
    tmp_path,
):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    first_record = signed_record("repo-a", owner.key.hex(), owner.address)
    record_with_source(memory, first_record)

    second_record = deepcopy(first_record)
    second_record["signed_fields"]["source_session_id"] = "session-two"
    second_record["signed_fields"]["incident_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    second_record["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(second_record["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()
    record_with_source(memory, second_record)

    changed_first = deepcopy(first_record)
    changed_first["signed_fields"]["checkpoint_spec"]["argv"] = [
        "python",
        "scripts/another_check.py",
    ]
    changed_first["intervention_signature"] = Account.sign_message(
        encode_defunct(text=intervention_message(changed_first["signed_fields"])),
        private_key=owner.key,
    ).signature.hex()

    with pytest.raises(
        MemoryIntegrityError,
        match="already has a different intervention",
    ):
        memory.record_intervention(changed_first)
    assert memory.all_lessons()[0]["intervention_count"] == 2


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
