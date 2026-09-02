from datetime import datetime, timezone

from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.identity import repository_identity
from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.signing import intervention_message


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
        "checkpoint_command": "pnpm run release:check",
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "codex-correction",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)), private_key=owner.key
    ).signature.hex()
    memory = InterventionMemory(db, repo_id)
    memory.record_intervention(
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
    assert run["checkpoint_command"] == "pnpm run release:check"


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
        "checkpoint_command": "pnpm run release:check",
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "codex-correction",
        "incident_at": datetime.now(timezone.utc).isoformat(),
    }
    signature = Account.sign_message(
        encode_defunct(text=intervention_message(signed_fields)), private_key=owner.key
    ).signature.hex()
    memory = InterventionMemory(db, repo_id)
    memory.record_intervention(
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
    lesson = memory.record_intervention(signed_record("repo-a", owner.key, owner.address))
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
        "checkpoint_command": "python scripts/release_check.py",
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
    memory.record_intervention(signed_record("repo-a", owner.key.hex(), owner.address))
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


def test_open_run_survives_followup_prompt(tmp_path):
    owner = Account.create()
    memory = InterventionMemory(tmp_path / "memory.db", "repo-a")
    memory.record_intervention(signed_record("repo-a", owner.key.hex(), owner.address))
    first = memory.start_run(
        session_id="same-session",
        task_class="release",
        area="release_workflow",
        agent_family="Codex",
        model="test",
    )
    memory.add_evidence("same-session", "release_check_passed")
    resumed = memory.start_run(
        session_id="same-session",
        task_class="low_risk",
        area="general",
        agent_family="Codex",
        model="test",
    )
    assert resumed["task_class"] == "release"
    assert resumed["satisfied_evidence"] == ["release_check_passed"]
