"""Doctor must reject altered lifecycle launchers before executing any prefix."""

from __future__ import annotations

import copy
import json
import os
import shlex
from pathlib import Path

import pytest

import comeback.diagnostics as diagnostics
import comeback.installer as installer
from comeback.identity import RepositoryConfig
from comeback.installer import claude_hook_groups, hook_groups


def configured_fixture(tmp_path, monkeypatch, agent):
    root = tmp_path / "repository"
    root.mkdir()
    tools = tmp_path / "trusted tools"
    tools.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    hook = tools / ("comeback-hook" + suffix)
    cli = tools / ("comeback" + suffix)
    hook.write_text("fixture hook", encoding="utf-8")
    cli.write_text("fixture capability", encoding="utf-8")
    hook.chmod(0o755)
    cli.chmod(0o755)
    if os.name == "nt":
        (tools / "python.exe").write_text("fixture interpreter", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "resolve_hook_executable", lambda **_kwargs: hook)
    monkeypatch.setattr(
        diagnostics, "repository_configuration",
        lambda _: RepositoryConfig(root=root, repo_id="diagnostic-repo", base_trust=None),
    )
    factory = hook_groups if agent == "codex" else claude_hook_groups
    # Match the installed JSON tree: factories may reuse a dict internally,
    # but separately configured lifecycle handlers must mutate independently.
    document = json.loads(json.dumps({"hooks": factory(hook)}))
    config = root / (".codex/hooks.json" if agent == "codex" else ".claude/settings.json")
    config.parent.mkdir()
    return root, config, document, cli


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_doctor_accepts_only_current_installation_canonical_launcher(tmp_path, monkeypatch, agent):
    _root, _config, document, cli = configured_fixture(tmp_path, monkeypatch, agent)
    for event_name, groups in document["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                assert diagnostics._capability_executable(handler, agent=agent, event_name=event_name) == str(cli)


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_doctor_uses_corresponding_lifecycle_timeout(tmp_path, monkeypatch, agent):
    _root, _config, document, cli = configured_fixture(tmp_path, monkeypatch, agent)
    for event_name, timeout in {"UserPromptSubmit": 30, "PreToolUse": 42, "Stop": 25}.items():
        document["hooks"][event_name][0]["hooks"][0]["timeout"] = timeout
    factory_name = "hook_groups" if agent == "codex" else "claude_hook_groups"
    monkeypatch.setattr(diagnostics, factory_name, lambda _hook: copy.deepcopy(document["hooks"]))
    for event_name, groups in document["hooks"].items():
        handler = groups[0]["hooks"][0]
        assert diagnostics._capability_executable(handler, agent=agent, event_name=event_name) == str(cli)
        altered = {**handler, "timeout": handler["timeout"] - 1}
        with pytest.raises(diagnostics.DiagnosticFailure) as failure:
            diagnostics._capability_executable(altered, agent=agent, event_name=event_name)
        assert failure.value.code == "HOOK_LAUNCHER_UNTRUSTED"


@pytest.mark.parametrize("agent", ["codex", "claude"])
@pytest.mark.parametrize("mutation", [
    "prefix", "suffix", "comment_with_valid_cli", "other_cli", "wrong_family",
    "windows_prefix", "async_stop", "duplicate", "wrong_matcher", "non_string",
    "non_string_windows", "duplicate_without_windows",
    "timeout_prompt", "timeout_pretool", "timeout_stop", "timeout_missing",
])
def test_doctor_rejects_modified_config_before_any_subprocess(tmp_path, monkeypatch, agent, mutation):
    root, config, document, cli = configured_fixture(tmp_path, monkeypatch, agent)
    marker = tmp_path / "unexpected-prefix-ran"
    prefix = "printf unexpected > " + shlex.quote(str(marker))
    for event, groups in document["hooks"].items():
        handler = groups[0]["hooks"][0]
        if mutation == "prefix":
            handler["command"] = prefix + "; " + handler["command"]
        elif mutation == "suffix":
            handler["command"] += "; " + prefix
        elif mutation == "comment_with_valid_cli":
            handler["command"] = prefix + " # comeback-hook --cli-executable " + shlex.quote(str(cli))
        elif mutation == "other_cli":
            other = cli.with_name("another-capability" + cli.suffix)
            other.write_text("fixture", encoding="utf-8")
            other.chmod(0o755)
            handler["command"] = handler["command"].replace(
                "--cli-executable " + shlex.quote(str(cli)),
                "--cli-executable " + shlex.quote(str(other)),
            )
        elif mutation == "wrong_family":
            old, new = ("Codex", "ClaudeCode") if agent == "codex" else ("ClaudeCode", "Codex")
            handler["command"] = handler["command"].replace(old, new)
        elif mutation == "windows_prefix":
            if agent == "claude":
                handler["command"] = prefix + " && " + handler["command"]
            else:
                handler["commandWindows"] = "Write-Output unexpected; " + handler["commandWindows"]
        elif mutation == "async_stop" and event == "Stop":
            handler["async"] = True
        elif mutation == "timeout_prompt" and event == "UserPromptSubmit":
            handler["timeout"] = 1
        elif mutation == "timeout_pretool" and event == "PreToolUse":
            handler["timeout"] = 1
        elif mutation == "timeout_stop" and event == "Stop":
            handler["timeout"] = 1
        elif mutation == "timeout_missing":
            handler.pop("timeout")
        elif mutation == "duplicate":
            groups[0]["hooks"].append(copy.deepcopy(handler))
        elif mutation == "wrong_matcher" and event == "PreToolUse":
            groups[0]["matcher"] = ".*"
        elif mutation == "non_string":
            handler["command"] = {"comeback-hook": "invalid"}
        elif mutation == "non_string_windows":
            if agent == "codex":
                handler["commandWindows"] = {"comeback-hook": "invalid"}
            else:
                handler["command"] = ["comeback-hook"]
        elif mutation == "duplicate_without_windows":
            additional = copy.deepcopy(handler)
            additional.pop("commandWindows", None)
            additional["command"] = prefix + "; " + additional["command"]
            groups[0]["hooks"].insert(0, additional)
    config.write_text(json.dumps(document), encoding="utf-8")

    def unexpected_process(*_args, **_kwargs):
        pytest.fail("doctor must reject altered hooks before starting a client or shell")

    monkeypatch.setattr(diagnostics.subprocess, "run", unexpected_process)
    result = diagnostics.diagnose_repository(root, agents=(agent,))
    assert result["gate"] == "FAIL"
    assert result["checks"][agent]["code"] in {
        "HOOK_LAUNCHER_UNTRUSTED", "HOOK_CONFIG_INVALID",
    }
    assert not marker.exists()


def test_direct_claude_probe_cannot_bypass_launcher_verification(tmp_path, monkeypatch):
    root, _config, document, _cli = configured_fixture(tmp_path, monkeypatch, "claude")
    handler = document["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    handler["command"] = "printf unexpected; " + handler["command"]

    def unexpected_process(*_args, **_kwargs):
        pytest.fail("untrusted prefix executed")

    monkeypatch.setattr(diagnostics.subprocess, "run", unexpected_process)
    with pytest.raises(diagnostics.DiagnosticFailure) as failure:
        diagnostics._invoke_installed_hook(
            root=root, handler=handler, agent="claude", database=tmp_path / "memory.db",
            session_id="fresh-diagnostic", temporary=tmp_path,
        )
    assert failure.value.code == "HOOK_LAUNCHER_UNTRUSTED"


def test_direct_claude_probe_uses_the_validated_lifecycle_timeout(tmp_path, monkeypatch):
    root, _config, document, _cli = configured_fixture(tmp_path, monkeypatch, "claude")
    handler = document["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    calls = []
    expected = {"hookSpecificOutput": {"additionalContext": "fixture context"}}

    def probe(argv, **kwargs):
        calls.append(kwargs["timeout"])
        return diagnostics.subprocess.CompletedProcess(argv, 0, json.dumps(expected), "")

    monkeypatch.setattr(diagnostics, "_git_bash", lambda: "fixture-git-bash")
    monkeypatch.setattr(diagnostics.subprocess, "run", probe)
    assert diagnostics._invoke_installed_hook(
        root=root, handler=handler, agent="claude", database=tmp_path / "memory.db",
        session_id="fresh-diagnostic", temporary=tmp_path,
    ) == expected
    assert calls == [handler["timeout"]] == [30]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_doctor_missing_current_launcher_does_not_trust_foreign_path_installation(tmp_path, monkeypatch, agent):
    root, config, document, cli = configured_fixture(tmp_path, monkeypatch, agent)
    config.write_text(json.dumps(document), encoding="utf-8")
    foreign_hook = cli.with_name("comeback-hook" + cli.suffix)
    monkeypatch.setattr(diagnostics, "resolve_hook_executable", installer.resolve_hook_executable)
    monkeypatch.setattr(installer.sys, "executable", str(tmp_path / "missing-install" / "python"))

    def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("doctor must not discover its trust root through PATH")

    def unexpected_process(*_args, **_kwargs):
        pytest.fail("doctor must reject missing current launcher before executing anything")

    # The ordinary installer can discover this valid foreign installation;
    # doctor must require the launcher beside its own Python instead.
    monkeypatch.setattr(installer.shutil, "which", lambda _name: str(foreign_hook))
    assert installer.resolve_hook_executable() == foreign_hook.resolve()
    monkeypatch.setattr(installer.shutil, "which", unexpected_lookup)
    monkeypatch.setattr(diagnostics.subprocess, "run", unexpected_process)
    result = diagnostics.diagnose_repository(root, agents=(agent,))
    assert result["gate"] == "FAIL"
    assert result["checks"][agent]["code"] == "CAPABILITY_EXECUTABLE_MISSING"
