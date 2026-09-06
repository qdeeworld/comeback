"""Independent signed workflow identities, not just prompt labels."""
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3
import shlex
import sys

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.execution import execute_checkpoint
from comeback.policy import classify_task
from comeback.signing import approval_message, intervention_message
from test_migration_workflow_spike import fixture, begin, child, event, MIGRATE


def add_deployment(root, owner, *, argv=None):
    with InterventionMemory(root / '.comeback/memory.db', 'repo-a') as memory:
        fields = deepcopy(memory.all_lessons()[0]['signed_fields'])
        fields.update(lesson_id='release-release_workflow-codex', area='release_workflow',
                      source_session_id='deployment-source')
        fields['release_spec']['argv'] = argv or [sys.executable, '-c', "print('deployment action')"]
        fields['checkpoint_spec']['argv'] = [sys.executable, '-c', "print('deployment check')"]
        memory.start_run(session_id='deployment-source', task_class='release', area='release_workflow',
                         agent_family='Codex', model='test')
        return memory.record_intervention({'signed_fields': fields,
            'intervention_signature': Account.sign_message(
                encode_defunct(text=intervention_message(fields)), owner.key).signature.hex(),
            'incident_summary': 'Deployment skipped its checks.'})


def test_workflows_have_separate_approvals_receipts_and_evolution(tmp_path):
    owner = fixture(tmp_path)
    add_deployment(tmp_path, owner)
    begin(tmp_path)
    with sqlite3.connect(tmp_path / '.comeback/application.db') as db:
        db.execute("UPDATE users SET email='b@example.invalid' WHERE id=2")
    child(tmp_path, 'checkpoint', session='migration-fresh')
    with InterventionMemory(tmp_path / '.comeback/memory.db', 'repo-a') as memory:
        deployment = memory.start_run(session_id='deployment-fresh', task_class='release',
            area='release_workflow', agent_family='Codex', model='test')
        migration = memory.get_run('migration-fresh')
        assert deployment['mode'] == migration['mode'] == 'HUMAN_REQUIRED'
        assert deployment['lesson_ids'] != migration['lesson_ids']
        assert deployment['checkpoint_receipt'] is None
        assert set(memory.missing_requirements(deployment)) == {'human_approval', 'release_check_passed'}
        result, code = execute_checkpoint(memory, session_id='deployment-fresh', root=tmp_path)
        assert code == 0 and result['decision'] == 'checkpoint_recorded'
        at = datetime.now(timezone.utc).isoformat()
        signature = Account.sign_message(encode_defunct(text=approval_message(migration, at)), owner.key).signature.hex()
        with pytest.raises(MemoryIntegrityError, match='not signed by the authorized closer'):
            memory.approve('deployment-fresh', approved_at=at, signature=signature)
        memory.approve('migration-fresh', approved_at=at, signature=signature)
    assert child(tmp_path, 'release', session='migration-fresh')['outcome'] == 'success'
    with InterventionMemory(tmp_path / '.comeback/memory.db', 'repo-a') as memory:
        migration = memory.matching_lessons('release', 'migration_workflow', 'Codex')[0]
        deployment = memory.matching_lessons('release', 'release_workflow', 'Codex')[0]
        assert migration['current_mode'] == 'CHECKPOINTED'
        assert deployment['current_mode'] == 'HUMAN_REQUIRED'
        assert deployment['success_count'] == 0
        assert migration['success_count'] == 1
        # Signed all_supported scope transfers only this workflow to Claude.
        assert memory.matching_lessons('release', 'migration_workflow', 'ClaudeCode')[0]['lesson_id'] == migration['lesson_id']


def test_session_cannot_switch_workflows_to_reuse_evidence(tmp_path):
    fixture(tmp_path)
    begin(tmp_path)
    denied = child(tmp_path, 'event', event=event(tmp_path, 'PreToolUse',
        tool_name='Bash', tool_use_id='different-workflow', tool_input={'command': 'git push origin main'}))
    assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert child(tmp_path, 'run', session='migration-fresh')['area'] == 'migration_workflow'


def test_same_protected_command_cannot_create_alternative_workflow(tmp_path):
    owner = fixture(tmp_path)
    with pytest.raises(MemoryIntegrityError, match='different workflows'):
        add_deployment(tmp_path, owner, argv=[sys.executable, '-c', MIGRATE])


def test_prompt_routes_scopes_without_relabeling_legacy_deployment():
    assert classify_task('Apply the database migration.') == ('release', 'migration_workflow')
    assert classify_task('Deploy this website.') == ('release', 'release_workflow')
    assert classify_task('Read the README.') == ('low_risk', 'general')


@pytest.mark.parametrize('prompt', [
    'Run migration tests', 'Run database migration checks',
    'Execute migration validation', 'Run migration unit tests',
    'Run migration-test suite',
    'Explain how to run the database migration.',
    'Do not run the database migration.',
    'Never apply the database migration.',
    'Run the database migration? Just explain, do not execute.',
    'Apply the database migration. Actually, do not run it.',
])
def test_migration_test_requests_are_not_protected_actions(prompt):
    assert classify_task(prompt) == ('low_risk', 'general')


@pytest.mark.parametrize('prompt', ['Run migration tests',
    'Explain how to run the database migration.', 'Do not run the database migration.'])
def test_test_only_session_can_stop_with_migration_lesson_present(tmp_path, prompt):
    fixture(tmp_path)
    child(tmp_path, 'event', event=event(tmp_path, 'UserPromptSubmit',
        session='tests-only', prompt=prompt))
    assert child(tmp_path, 'run', session='tests-only')['lesson_ids'] == []
    assert child(tmp_path, 'event', event=event(tmp_path, 'Stop', session='tests-only')) is None


def test_signed_action_selects_migration_without_prompt_keyword(tmp_path):
    fixture(tmp_path)
    child(tmp_path, 'event', event=event(tmp_path, 'UserPromptSubmit',
        session='action-selected', prompt='Update the profile feature.'))
    assert child(tmp_path, 'run', session='action-selected')['lesson_ids'] == []
    result = child(tmp_path, 'event', event=event(tmp_path, 'PreToolUse',
        session='action-selected', tool_name='Bash', tool_use_id='actual-migration',
        tool_input={'command': shlex.join([sys.executable, '-c', MIGRATE])}))
    assert result['hookSpecificOutput']['permissionDecision'] == 'deny'
    run = child(tmp_path, 'run', session='action-selected')
    assert run['area'] == 'migration_workflow'
    assert run['lesson_ids'] == ['release-migration_workflow-codex']
