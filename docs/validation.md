# Reproducing validation

[Back to Comeback](../README.md) · [Installation](installation.md) · [Workflows](workflows.md) · [Base](base.md) · [Security](security.md) · [Validation](validation.md)

## Versioned results

- [September 10 public demo](https://youtu.be/jgZ2JFGiydE): Comeback runtime `3db9acc9efdd159d881fe903f9a354b6d99281c7`, Codex CLI `0.153.4`, disposable local release. Its separate Base insert is a live contract-backed capability preflight, not a new onchain transaction or production release.
- [September 10 authenticated Codex restart regression](../evidence/codex-resume-execution-2026-09-10.md): same-thread receipt/approval continuity across CLI processes; not full hook onboarding.
- [September 6 Codex and Windows Claude validation](../evidence/agent-validation-2026-09-06.md): exact releases, sessions and fixture boundaries, including the earlier migration check.
- [September 4 active-memory journey](../evidence/active-memory-gate-2026-09-04.md): earlier authenticated Codex runs with an active Base anchor; these historical results are not fresh validation of every later change.

Passing a current unit test or editing a validator does not establish authenticated compatibility. Read the revision and environment attached to each result. These are implementation checks, not independent adoption or measured developer time savings.

## Verify a development clone

Run these commands from the development clone's root. With `uv`, this setup works without a preinstalled Python.

macOS or Linux:

```bash
uv venv .uvenv --python 3.13
uv pip install -e '.[dev]' --python .uvenv/bin/python
.uvenv/bin/python -m pytest -q
.uvenv/bin/python scripts/run_validation_gate.py
.uvenv/bin/python scripts/run_installed_hook_gate.py
.uvenv/bin/python scripts/run_codex_hook_gate.py
```

Windows PowerShell:

```powershell
$env:UV_LINK_MODE = "copy"
uv venv .uvenv --python 3.13
uv pip install -e ".[dev]" --python .uvenv\Scripts\python.exe
.uvenv\Scripts\python.exe -m pytest -q
.uvenv\Scripts\python.exe scripts\run_validation_gate.py
.uvenv\Scripts\python.exe scripts\run_installed_hook_gate.py
.uvenv\Scripts\python.exe scripts\run_codex_hook_gate.py
```

This **development-clone** setup is separate from the [README's `uv tool install` user installation](../README.md#1-install). For Claude on Windows, run the authenticated gates from Git Bash in a normal terminal outside any running Claude session. Use a short disposable clone path outside Claude's scratchpad; nested-session sandbox execution restrictions are not proof of a Comeback hook denial. In Git Bash, set `export UV_LINK_MODE=copy` before installing and use `.uvenv/Scripts/python.exe`.

The deterministic gate proves five fresh-session denials, a simulated Codex-to-Claude scope transition, malicious-prompt resistance, low-risk autonomy, signed checkpoint/approval/release, evolving supervision, and memory ablation. The installed-hook gate uses the generated POSIX command for Claude and the generated `commandWindows` through native PowerShell and `cmd.exe` for Codex. The real Codex gate proves a real Codex source session and a separate fresh Codex denial. Its setup then completes a signed `HUMAN_REQUIRED` capability run directly before a final fresh Codex process exercises the evolved `CHECKPOINTED` capability; it is activation and enforcement evidence, not one unbroken all-agent-driven approval journey.

The default test suite also exercises checkpoint → signed test-owner approval → release in separate Python processes with different Codex shim PATH entries. An optional authenticated restart regression uses two actual Codex CLI invocations (`exec`, then `exec resume`) and requires the original receipt and approval to remain unchanged. Run it with `COMEBACK_TEST_AUTHENTICATED_CODEX=1 python -m pytest tests/test_codex_resume_execution.py -q -s` (PowerShell: set `$env:COMEBACK_TEST_AUTHENTICATED_CODEX = '1'` first). It consumes model turns and uses a disposable repository and generated test owner. It validates capability execution across a real CLI restart, **not** hook activation, native owner-keystore onboarding or independent usage.

The real Codex gate requires an authenticated local Codex CLI. Its explicit trust override and hook-trust bypass apply only to its newly created disposable repository; they are not the user onboarding path.

Run the real Claude gates separately only on a machine with authenticated Claude Code. They are not part of `comeback doctor --agent claude`:

macOS or Linux:

```bash
.uvenv/bin/python scripts/run_cross_agent_gate.py
.uvenv/bin/python scripts/run_claude_unlock_gate.py
```

Windows Git Bash:

```bash
.uvenv/Scripts/python.exe scripts/run_cross_agent_gate.py
.uvenv/Scripts/python.exe scripts/run_claude_unlock_gate.py
```

The first authenticated Claude gate requires a genuinely fresh Claude session to recall a seeded Codex intervention, an exact session/tool/command match between Claude's denial and Sibyl's own denial record, and an absent release side effect. The second first seeds an owner-approved capability success directly, then requires a genuinely fresh Claude process to recall the evolved `CHECKPOINTED` mode, invoke its checkpoint and release capabilities, create the side effect, and store success. Both scripts use Claude's permission-skipping option only inside their disposable fixtures. Neither claims interactive permission onboarding, a real Codex source process or Claude performing the earlier owner approval. Inspect the versioned evidence before treating a changed validator as an authenticated result.

The unlock gate captures Claude's [verbose JSON event stream](https://code.claude.com/docs/en/headless#stream-responses). It matches Bash tool IDs, exact command hashes, results, and session identity against Sibyl permissions. One checkpoint retry is accepted only with an explicit tool-level exit-126 permission error, exactly one actual checkpoint start/pass in Sibyl, a one-shot checkpoint execution witness, and successful checkpoint/release results. More retries, duplicate execution, missing results, unrelated commands, denials, or model-written explanations alone fail the gate. This is bounded prototype verification, not a general shell sandbox.

Run the gate from a normal terminal; it refuses a detected nested Claude session before consuming authenticated turns. Save its complete JSON output, including `raw_claude_stream` and `execution_trace_error`, privately for diagnosis. Offline trace tests validate the checker, not authenticated Claude compatibility on a new version. A timeout is a failure and retains captured output; do not rerun repeatedly or patch the gate to force PASS.

`COMEBACK_MEMORY_DB` is an absolute-path-only diagnostic/development override. Both lifecycle hooks and normal CLI commands resolve it consistently, `comeback status` prints the authoritative selected database and whether an override is active, and hook-injected capability commands carry that exact database with `--db`. Leave the variable unset for the normal repository-local `.comeback/memory.db` journey. If a diagnostic intentionally exports it, use that same exported value—or the printed explicit `--db` path—for every operator-side `status`, `intervene`, `approve`, and `reconcile` command.

GitHub Actions runs the unit, deterministic memory, and installed-launcher gates on Linux and Windows. Authenticated real-agent gates remain release checks outside CI.
