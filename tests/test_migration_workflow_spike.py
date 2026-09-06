"""Bounded SQLite experiment, not a migration engine or real-agent evidence.

Uses today's release-class signed capability without extending the schema.
Database contents live outside the repository fingerprint: stale DB state is
explicitly tested as a limitation, not claimed as protected.
"""
import json
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from comeback.memory import InterventionMemory, MemoryIntegrityError
from comeback.signing import approval_message


CHECK = """import sqlite3
with sqlite3.connect('.comeback/application.db') as db:
    duplicate = db.execute('SELECT email FROM users GROUP BY email HAVING COUNT(*) > 1').fetchone()
    assert duplicate is None, 'duplicate emails must be resolved before migration'
"""
MIGRATE = """import sqlite3
with sqlite3.connect('.comeback/application.db') as db:
    db.execute('CREATE UNIQUE INDEX users_email_unique ON users(email)')
"""


def fixture(root):
    # Session one's writer exits completely before any recall process starts.
    # This generated test-only signer is unfunded and never used externally.
    code = """
import json, sqlite3, sys
from pathlib import Path
data = json.load(sys.stdin)
sys.path.insert(0, data['tests'])
from test_execution import _supervised_memory
root = Path.cwd()
memory, owner = _supervised_memory(root,
    checkpoint_argv=[sys.executable, '-c', data['check']],
    release_argv=[sys.executable, '-c', data['migrate']])
with sqlite3.connect(root / '.comeback/application.db') as db:
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT)')
    db.executemany('INSERT INTO users(email) VALUES (?)', [('a@example.invalid',)] * 2)
memory.close()
print(owner.key.hex())
"""
    completed = subprocess.run([sys.executable, '-c', code], cwd=root,
        input=json.dumps({'tests': str(Path(__file__).parent), 'check': CHECK, 'migrate': MIGRATE}),
        capture_output=True, text=True, timeout=40)
    assert completed.returncode == 0, completed.stderr
    return Account.from_key(completed.stdout.strip())


def child(root, operation, **values):
    # Each event/capability runs in a new process; no injected conversation or
    # remembered lesson is supplied to these processes.
    code = """
import json, sys
from pathlib import Path
from comeback.memory import InterventionMemory
from comeback.hook import _handle_event
from comeback.execution import execute_checkpoint, execute_release
data = json.load(sys.stdin)
with InterventionMemory(Path(data['database']), 'repo-a') as memory:
    operation = data['operation']
    if operation == 'event':
        result = _handle_event(data['event'], root=Path.cwd(), memory=memory)
    elif operation == 'run':
        result = memory.get_run(data['session'])
    else:
        fn = execute_checkpoint if operation == 'checkpoint' else execute_release
        result, status = fn(memory, session_id=data['session'], root=Path.cwd())
    print(json.dumps(result))
"""
    completed = subprocess.run([sys.executable, '-c', code], cwd=root,
        input=json.dumps({'operation': operation, 'database': str(root / '.comeback/memory.db'), **values}),
        capture_output=True, text=True, timeout=40)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def event(root, name, session='migration-fresh', **extra):
    return {'hook_event_name': name, 'session_id': session,
            '_comeback_agent_family': 'Codex',
            '_comeback_memory_db': str(root / '.comeback/memory.db'), **extra}


def begin(root):
    child(root, 'event', event=event(root, 'UserPromptSubmit', prompt='Apply the database migration.'))
    return child(root, 'event', event=event(root, 'PreToolUse',
        tool_name='Bash', tool_use_id='migration-command',
        tool_input={'command': shlex.join([sys.executable, '-c', MIGRATE])}))


def indexes(root):
    with sqlite3.connect(root / '.comeback/application.db') as db:
        return db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()


def test_configured_migration_fresh_process_denial_check_approval_and_completion(tmp_path):
    owner = fixture(tmp_path)
    denial = begin(tmp_path)
    assert denial['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert indexes(tmp_path) == []
    run = child(tmp_path, 'run', session='migration-fresh')
    assert run['mode'] == 'HUMAN_REQUIRED'
    assert child(tmp_path, 'checkpoint', session='migration-fresh')['decision'] == 'checkpoint_failed'
    assert indexes(tmp_path) == []
    with sqlite3.connect(tmp_path / '.comeback/application.db') as db:
        db.execute("UPDATE users SET email='b@example.invalid' WHERE id=2")
    assert child(tmp_path, 'checkpoint', session='migration-fresh')['decision'] == 'checkpoint_recorded'
    with InterventionMemory(tmp_path / '.comeback/memory.db', 'repo-a') as memory:
        run = memory.get_run('migration-fresh')
        assert memory.missing_requirements(run) == ['human_approval']
        approved_at = datetime.now(timezone.utc).isoformat()
        message = encode_defunct(text=approval_message(run, approved_at))
        stranger = Account.create()
        with pytest.raises(MemoryIntegrityError):
            memory.approve('migration-fresh', approved_at=approved_at,
                signature=Account.sign_message(message, stranger.key).signature.hex())
        memory.approve('migration-fresh', approved_at=approved_at,
            signature=Account.sign_message(message, owner.key).signature.hex())
    result = child(tmp_path, 'release', session='migration-fresh')
    assert result['outcome'] == 'success'
    assert indexes(tmp_path) == [('users_email_unique',)]
    assert child(tmp_path, 'run', session='migration-fresh')['outcome'] == 'success'


def test_unrelated_action_and_memory_ablation(tmp_path):
    fixture(tmp_path)
    child(tmp_path, 'event', event=event(tmp_path, 'UserPromptSubmit', prompt='Read the README.'))
    unrelated = child(tmp_path, 'event', event=event(tmp_path, 'PreToolUse',
        tool_name='Bash', tool_use_id='read', tool_input={'command': 'git status --short'}))
    assert unrelated is None
    assert begin(tmp_path)['hookSpecificOutput']['permissionDecision'] == 'deny'
    # Recoverable removal of the whole local store after all handles/processes
    # close. No Base anchor is configured in this explicit ablation fixture.
    store = tmp_path / '.comeback/memory.db'
    store.rename(tmp_path / '.comeback/memory.saved.db')
    decision = begin(tmp_path)
    assert decision is None  # remembered command-specific interception is absent
    assert indexes(tmp_path) == []  # ablation never executes the unapproved action


def test_current_migration_obligation_also_gates_unrelated_deployment(tmp_path):
    """Characterize the missing independent workflow scope; not a desired API."""
    fixture(tmp_path)
    child(tmp_path, 'event', event=event(tmp_path, 'UserPromptSubmit',
        session='deployment', prompt='Deploy this website.'))
    decision = child(tmp_path, 'event', event=event(tmp_path, 'PreToolUse',
        session='deployment', tool_name='Bash', tool_use_id='push',
        tool_input={'command': 'git push origin main'}))
    assert decision['hookSpecificOutput']['permissionDecision'] == 'deny'
    run = child(tmp_path, 'run', session='deployment')
    assert run['lesson_ids'] == ['release-release_workflow-codex']
    assert run['mode'] == 'HUMAN_REQUIRED'


def test_database_changes_do_not_invalidate_current_checkpoint_receipt(tmp_path):
    """Expose the external-state gap without running a stale approved migration."""
    owner = fixture(tmp_path)
    begin(tmp_path)
    with sqlite3.connect(tmp_path / '.comeback/application.db') as db:
        db.execute("UPDATE users SET email='b@example.invalid' WHERE id=2")
    assert child(tmp_path, 'checkpoint', session='migration-fresh')['decision'] == 'checkpoint_recorded'
    with InterventionMemory(tmp_path / '.comeback/memory.db', 'repo-a') as memory:
        run = memory.get_run('migration-fresh')
        approved_at = datetime.now(timezone.utc).isoformat()
        memory.approve('migration-fresh', approved_at=approved_at,
            signature=Account.sign_message(
                encode_defunct(text=approval_message(run, approved_at)), owner.key).signature.hex())
    with sqlite3.connect(tmp_path / '.comeback/application.db') as db:
        db.execute("UPDATE users SET email='a@example.invalid' WHERE id=2")
    with InterventionMemory(tmp_path / '.comeback/memory.db', 'repo-a') as memory:
        run = memory.get_verified_run('migration-fresh')
        assert memory.missing_requirements(run) == []
        assert run['checkpoint_receipt'] is not None
    # The configured verifier now fails despite the retained receipt. A safe
    # migration must validate/lock DB state transactionally itself.
    check = subprocess.run([sys.executable, '-c', CHECK], cwd=tmp_path,
                           capture_output=True, text=True, timeout=10)
    assert check.returncode != 0
    assert indexes(tmp_path) == []
