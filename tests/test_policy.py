from comeback.policy import classify_task, mode_for_outcomes


def test_task_classification_keeps_unrelated_work_autonomous():
    assert classify_task("Update the README wording") == ("low_risk", "general")
    assert classify_task("Deploy the release to production") == (
        "release",
        "release_workflow",
    )


def test_supervision_evolves_with_outcomes():
    assert mode_for_outcomes(1, 0) == "HUMAN_REQUIRED"
    assert mode_for_outcomes(1, 1) == "CHECKPOINTED"
    assert mode_for_outcomes(1, 3) == "AUTONOMOUS"

