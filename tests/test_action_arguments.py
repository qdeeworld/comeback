import pytest

from comeback.memory import MemoryIntegrityError, _action_spec, _argv, _strings


def test_signed_command_preserves_repeated_arguments_and_order():
    argv = ["python", "verify.py", "--include", "src", "--include", "tests", "src"]
    result = _action_spec({"argv": argv, "timeout_seconds": 60}, "checkpoint_spec")
    assert result["argv"] == argv
    assert result["argv"] is not argv


@pytest.mark.parametrize("argv", [[], [""], ["python", ""], ["python", 1], "python"])
def test_argument_validation_still_rejects_invalid_vectors(argv):
    with pytest.raises(MemoryIntegrityError):
        _argv(argv, "checkpoint_spec.argv")


def test_permission_sets_still_reject_duplicate_requirements():
    with pytest.raises(MemoryIntegrityError, match="duplicates"):
        _strings(["human_approval", "human_approval"], "required_evidence")


def test_repeated_credential_arguments_are_not_persisted():
    with pytest.raises(MemoryIntegrityError, match="credential-bearing"):
        _action_spec(
            {"argv": ["python", "verify.py", "--token", "not-a-secret", "--token", "not-a-secret"],
             "timeout_seconds": 60},
            "checkpoint_spec",
        )
