from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.hook import handle
from comeback.identity import repository_identity
from comeback.memory import InterventionMemory
from comeback.signing import intervention_message


def test_only_signed_repository_checkpoint_satisfies_gate(tmp_path: Path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setenv("COMEBACK_MEMORY_DB", str(db))
    owner = Account.create()
    _, repo_id = repository_identity(tmp_path)
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "severity": "release_blocker",
        "checkpoint_command": "pnpm run release:check",
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "corrected-session",
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
            "incident_summary": "The agent skipped the repository release check.",
        }
    )
    common = {
        "session_id": "fresh-session",
        "cwd": str(tmp_path),
        "model": "test",
        "permission_mode": "default",
    }
    handle({**common, "hook_event_name": "UserPromptSubmit", "prompt": "Deploy this release."})
    handle(
        {
            **common,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "python scripts/release_check.py"},
            "tool_response": {"exit_code": 0},
        }
    )
    assert memory.get_run("fresh-session")["satisfied_evidence"] == []

    handle(
        {
            **common,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pnpm run release:check"},
            "tool_response": {"exit_code": 0},
        }
    )
    assert memory.get_run("fresh-session")["satisfied_evidence"] == [
        "release_check_passed"
    ]


def test_release_action_promotes_vague_prompt_and_recalls_lesson(tmp_path: Path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setenv("COMEBACK_MEMORY_DB", str(db))
    owner = Account.create()
    _, repo_id = repository_identity(tmp_path)
    signed_fields = {
        "lesson_id": "release-release_workflow-codex",
        "repo_id": repo_id,
        "task_class": "release",
        "area": "release_workflow",
        "agent_family": "Codex",
        "severity": "release_blocker",
        "checkpoint_command": "./scripts/check-milestone.sh",
        "required_evidence": ["release_check_passed", "human_approval"],
        "authorized_closer": owner.address.lower(),
        "source_session_id": "corrected-session",
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
            "incident_summary": "A vague request led to an unsafe release attempt.",
        }
    )
    common = {
        "session_id": "vague-session",
        "cwd": str(tmp_path),
        "model": "test",
        "permission_mode": "default",
    }
    handle({**common, "hook_event_name": "UserPromptSubmit", "prompt": "Go on."})
    blocked = handle(
        {
            **common,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "forge script Deploy.s.sol --broadcast"},
        }
    )
    run = memory.get_run("vague-session")
    reason = blocked["hookSpecificOutput"]["permissionDecisionReason"]
    assert run["task_class"] == "release"
    assert run["checkpoint_command"] == "./scripts/check-milestone.sh"
    assert "remembered intervention requires" in reason


def test_claude_hook_recalls_cross_agent_codex_intervention(tmp_path: Path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setenv("COMEBACK_MEMORY_DB", str(db))
    monkeypatch.setenv("COMEBACK_AGENT_FAMILY", "ClaudeCode")
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
    InterventionMemory(db, repo_id).record_intervention(
        {
            "signed_fields": signed_fields,
            "intervention_signature": signature,
            "incident_summary": "Codex skipped the release check.",
        }
    )
    common = {
        "session_id": "fresh-claude",
        "cwd": str(tmp_path),
        "model": "claude",
    }

    handle({**common, "hook_event_name": "UserPromptSubmit", "prompt": "Deploy this release."})
    blocked = handle(
        {
            **common,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
        }
    )

    run = InterventionMemory(db, repo_id).get_run("fresh-claude")
    assert run["agent_family"] == "ClaudeCode"
    assert run["lesson_ids"] == ["release-release_workflow-codex"]
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
