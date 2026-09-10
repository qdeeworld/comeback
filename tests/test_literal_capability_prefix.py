"""Focused regression tests: no remote, credentials, or release is used."""

import os
import subprocess

import pytest

from comeback.policy import invocation_matches, is_release_capability


@pytest.mark.parametrize("expression", ["$(true)", "`true`", "%PATH%", "!PATH!", "^x"])
@pytest.mark.parametrize("action", ["checkpoint", "release"])
def test_evaluated_path_component_cannot_be_erased_by_dotdot(tmp_path, expression, action):
    expected = f"/trusted/bin/comeback {action} --session-id expected"
    command = f'cd "{tmp_path}/child{expression}/.." && {expected}'
    assert not invocation_matches(command, expected, working_directory=tmp_path)
    assert not is_release_capability(command, expected, working_directory=tmp_path)


@pytest.mark.parametrize("target", ["child*/..", "child?/..", "$HOME/..", "~", "child\nx/.."])
def test_expanding_or_control_character_paths_are_not_literal(tmp_path, target):
    expected = "/trusted/bin/comeback checkpoint --session-id expected"
    assert not invocation_matches(
        f"cd {target} && {expected}", expected, working_directory=tmp_path
    )


@pytest.mark.parametrize("separator", ["&&", ";"])
@pytest.mark.parametrize("prefix", ["plain", "single", "double", "cmd", "powershell", "relative"])
def test_supported_literal_same_directory_prefixes_still_work(tmp_path, separator, prefix):
    expected = "/trusted/bin/comeback checkpoint --session-id expected"
    heads = {
        "plain": f"cd {tmp_path}",
        "single": f"cd '{tmp_path}'",
        "double": f'cd "{tmp_path}"',
        "cmd": f'cd /d "{tmp_path}"',
        "powershell": f'Set-Location -LiteralPath "{tmp_path}"',
        "relative": "cd .",
    }
    assert invocation_matches(
        f"{heads[prefix]} {separator} {expected}",
        expected,
        working_directory=tmp_path,
    )


@pytest.mark.parametrize("quote", ["'", '"'])
def test_literal_space_path_still_works(tmp_path, quote):
    root = tmp_path / "path with spaces"
    root.mkdir()
    expected = "/trusted/bin/comeback checkpoint --session-id expected"
    assert invocation_matches(
        f"cd {quote}{root}{quote} && {expected}", expected, working_directory=root
    )


def test_unprefixed_command_preserved_but_append_and_wrong_dir_rejected(tmp_path):
    expected = "/trusted/bin/comeback release --session-id expected"
    assert invocation_matches(expected, expected, working_directory=tmp_path)
    assert not invocation_matches(
        f"{expected} && echo extra", expected, working_directory=tmp_path
    )
    assert not invocation_matches(
        f"cd {tmp_path}/child && {expected}", expected, working_directory=tmp_path
    )


@pytest.mark.skipif(os.name == "nt", reason="Harmless POSIX-shell semantics witness")
def test_rejected_prefix_would_have_a_real_shell_side_effect(tmp_path):
    (tmp_path / "child").mkdir()
    expected = "printf CAPABILITY_PLACEHOLDER"
    command = f'cd "{tmp_path}/child$(touch PREFIX_SIDE_EFFECT)/.." && {expected}'
    assert not invocation_matches(command, expected, working_directory=tmp_path)
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.stdout == "CAPABILITY_PLACEHOLDER"
    assert (tmp_path / "PREFIX_SIDE_EFFECT").is_file()


@pytest.mark.parametrize("operator", ["&", "|", "<", ">", "(", ")"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_control_operator_cannot_be_erased_by_dotdot(tmp_path, operator, quote):
    expected = "/trusted/bin/comeback release --session-id expected"
    command = f"cd {quote}{tmp_path}/child{operator}suffix/..{quote} && {expected}"
    assert not invocation_matches(command, expected, working_directory=tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Harmless native cmd.exe semantics witness")
def test_single_quotes_do_not_protect_cmd_prefix_from_side_effects(tmp_path):
    expected = "echo CAPABILITY_PLACEHOLDER"
    command = (
        f"cd '{tmp_path}/child & mkdir PREFIX_SIDE_EFFECT & rem /..' && {expected}"
    )
    # Unlike POSIX and PowerShell, cmd.exe does not treat single quotes as
    # quoting. The ampersands still separate commands before the capability.
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (tmp_path / "PREFIX_SIDE_EFFECT").is_dir(), result.stdout + result.stderr
    assert not invocation_matches(command, expected, working_directory=tmp_path)
