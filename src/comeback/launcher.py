"""Exact launch arguments shared by installed hooks and capabilities."""
from pathlib import Path
import subprocess


def launcher_argv(executable: Path, module: str) -> list[str]:
    executable = executable.expanduser().absolute()
    if executable.suffix.lower() != ".exe":
        return [str(executable)]
    # Windows console-script stubs have per-install hashes and can be rejected
    # independently by application control. Use the SAME environment's Python,
    # never a PATH fallback. Isolation prevents repository module shadowing.
    python = executable.with_name("python.exe")
    # A base Python installation puts entry points in Scripts but its interpreter
    # one level above. Never escape a venv to another/base interpreter.
    if (not python.is_file() and executable.parent.name.lower() == "scripts"
            and not (executable.parent.parent / "pyvenv.cfg").exists()):
        python = executable.parent.parent / "python.exe"
    if not python.is_file():
        raise RuntimeError(f"Comeback launcher requires its environment interpreter: {python}")
    return [str(python), "-I", "-m", module]


def preflight_launcher(hook: Path) -> None:
    """Probe imports/CLI startup without memory writes or authenticated agents."""
    if hook.suffix.lower() != ".exe":
        return
    argv = launcher_argv(hook, "comeback.cli") + ["--help"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Comeback interpreter preflight failed: {exc}. Do not start an agent or "
            "disable Windows security. Check application-control logs and use an "
            "administrator-approved Python installation."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Comeback interpreter preflight failed (exit {result.returncode}): "
            f"{result.stderr[-2000:]}. Do not start an agent or disable Windows security. "
            "Check application-control logs and the environment installation."
        )
