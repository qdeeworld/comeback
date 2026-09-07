from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import comeback.capture as capture
from comeback.cli import _confirm_signature
from comeback.memory import InterventionMemory, MemoryIntegrityError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    owner = Account.create()
    monkeypatch.setattr(capture.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(capture, "owner_address", lambda _: owner.address.lower())
    signed = []

    def sign(_path, message):
        signed.append(message)
        return Account.sign_message(encode_defunct(text=message), private_key=owner.key).signature.hex()

    monkeypatch.setattr(capture, "sign_with_owner", sign)
    with InterventionMemory(tmp_path / "memory.db", "repo-a") as memory:
        memory.start_run(session_id="corrected", task_class="release",
                         area="release_workflow", agent_family="Codex", model="test")
        yield memory, signed


def answers(monkeypatch, values):
    responses = iter(values)
    monkeypatch.setattr("builtins.input", lambda: next(responses))


def invoke(memory, session_id="corrected"):
    return capture.capture_correction(memory, repo_id="repo-a", keystore=Path("unused"),
                                      session_id=session_id, confirm=_confirm_signature)


def inputs(executable="python"):
    return ["Missed verifier", "all_supported", executable, "-m", "pytest", "",
            "git", "push", "https://example.test/repo.git", "HEAD:refs/heads/main", "", "SIGN"]


@pytest.mark.parametrize("executable", ["python", r"C:\Program Files\Python\python.exe", r"C:\Users\me\python.exe"])
def test_capture_preserves_exact_arguments_and_signed_scope(setup, monkeypatch, executable):
    memory, signed = setup
    answers(monkeypatch, ["corrected", *inputs(executable)])
    lesson = invoke(memory, session_id=None)
    assert len(signed) == 1
    assert lesson["checkpoint_spec"]["argv"] == [executable, "-m", "pytest"]
    assert lesson["agent_scope"] == "all_supported"
    run = memory.start_run(session_id="fresh-claude", task_class="release",
                           area="release_workflow", agent_family="ClaudeCode", model="test")
    explanation = capture.explain_session(memory, run["session_id"])
    assert set(explanation["recorded_evidence_still_missing"]) == {"release_check_passed", "human_approval"}
    assert "not release authorization" in explanation["notice"]


@pytest.mark.parametrize("values, error", [
    ([":cancel"], "cancelled"),
    (["", "all_supported"], "value is required"),
    (["Missed verifier", "everyone"], "Choose same_agent"),
    ([*inputs()[:-1], "NO"], "confirmation was not granted"),
])
def test_capture_cancellation_and_bad_input_never_sign_or_write(setup, monkeypatch, values, error):
    memory, signed = setup
    answers(monkeypatch, values)
    with pytest.raises(MemoryIntegrityError, match=error):
        invoke(memory)
    assert not signed
    assert not memory.all_lessons()


def test_capture_rejects_shell_wrapper_before_signing(setup, monkeypatch):
    memory, signed = setup
    answers(monkeypatch, ["Missed check", "same_agent", "bash", "-c", "true", "",
                          "git", "push", "origin", "main", ""])
    with pytest.raises(MemoryIntegrityError):
        invoke(memory)
    assert not signed
    assert not memory.all_lessons()


def test_capture_refuses_piped_input(setup, monkeypatch):
    memory, signed = setup
    monkeypatch.setattr(capture.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(MemoryIntegrityError, match="native terminal"):
        invoke(memory)
    assert not signed


def test_capture_refuses_wrong_session_without_guessing(setup):
    memory, signed = setup
    with pytest.raises(MemoryIntegrityError, match="no Sibyl supervision run"):
        invoke(memory, "nonexistent")
    assert not signed


def test_explanation_is_read_only_and_not_a_release_permission(setup, monkeypatch):
    memory, _ = setup
    before = memory.get_run("corrected")
    explanation = capture.explain_session(memory, "corrected")
    assert "No matching correction" in explanation["why"]
    assert memory.get_run("corrected") == before
