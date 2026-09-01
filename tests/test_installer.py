import json
from importlib.resources import files
from pathlib import Path

from comeback.installer import install_repository


def test_install_is_idempotent_and_preserves_other_hooks(tmp_path: Path):
    executable = tmp_path / "comeback-hook"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "other-policy"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    first = install_repository(tmp_path, executable=executable)
    second = install_repository(tmp_path, executable=executable)
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    pretool = installed["hooks"]["PreToolUse"]

    assert first == second
    assert len(pretool) == 2
    assert pretool[0]["hooks"][0]["command"] == "other-policy"
    assert "comeback-hook" in pretool[1]["hooks"][0]["command"]
    assert (tmp_path / ".agents" / "skills" / "release-safety" / "SKILL.md").exists()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".comeback/\n"


def test_packaged_skill_matches_repository_skill():
    packaged = files("comeback.assets").joinpath("release-safety.SKILL.md").read_text(encoding="utf-8")
    repository = (
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / "release-safety"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert packaged == repository

