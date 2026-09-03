# Comeback

[![Comeback validation](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml/badge.svg)](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml)

Comeback remembers where a coding agent needed human intervention, then changes how much autonomy the next agent receives for a comparable, configured repository release.

This repository is a bounded Sibyl hackathon validation spike, not a production security product. Its one implemented task class is repository release work:

1. A developer records a signed intervention after an agent skips a required release check.
2. Sibyl stores the intervention, command specifications, provenance, outcome counts, and current supervision mode.
3. The process ends.
4. A fresh Codex or Claude Code session receives only a related release request.
5. Comeback recalls the intervention and selects `HUMAN_REQUIRED`, `CHECKPOINTED`, or `AUTONOMOUS`.
6. Its hook denies recognized raw release commands; the one exact release argument vector recorded in the intervention can run through a one-shot Comeback capability after its requirements pass.
7. The result is written back to Sibyl, changing the next fresh session's supervision mode.

Unrelated low-risk work remains `AUTONOMOUS`.

## Prerequisites

- Git and a Git repository with at least one commit.
- An installed and authenticated coding agent. Codex CLI `0.152.1` is the version exercised by the authenticated gate. The externally reported `0.150.0-alpha.12.2` Windows build is not supported; regardless of version, `comeback doctor` must prove real lifecycle activation before use.
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/). It can install the required Python automatically.
- Git Bash only when using Claude Code on Windows.

`comeback init` requires a normal Git working tree with at least one commit. It refuses non-Git directories, repositories without `HEAD`, and linked Git worktrees before writing installation files. Install in a normal clone until project-hook discovery in linked worktrees is independently proven.

## Install on Windows PowerShell

Install `uv`, close and reopen PowerShell so `uv` is on `PATH`, then install Comeback with Python 3.13:

```powershell
winget install --id=astral-sh.uv -e --source winget --scope user
# Close this PowerShell window, open a new one, then continue.
uv python install 3.13
uv tool install --python 3.13 "git+https://github.com/qdeeworld/comeback.git"
uv tool update-shell
# Close and reopen PowerShell again so `comeback` is on PATH.
cd C:\path\to\your-repository
comeback init --agent codex
```

If `winget` is unavailable, use one of the other Windows installation methods in the official `uv` documentation linked above. Python, `pipx`, and `python3-venv` do not need to be installed separately when `uv` manages Python.

## Install on macOS or Linux

Install `uv` using its official installer or package-manager instructions, then run:

```bash
uv python install 3.13
uv tool install --python 3.13 "git+https://github.com/qdeeworld/comeback.git"
uv tool update-shell
# Close and reopen the terminal so `comeback` is on PATH.
cd /path/to/your-repository
comeback init --agent codex
```

`comeback init` writes two kinds of files:

- Review and commit the portable repository files: `.comeback-repository.json`, `.agents/skills/release-safety/SKILL.md`, and the `.gitignore` change for `.comeback/`.
- Review but do not commit the selected machine-local launcher: `.codex/hooks.json` for `--agent codex`, `.claude/settings.json` for `--agent claude`, or both files for `--agent both`. They contain absolute paths to this clone's installed `comeback-hook` and `comeback` executables. When Comeback creates an untracked launcher file, it adds that file to this clone's `.git/info/exclude` without changing the shared `.gitignore`.

It merges unrelated hook entries and refuses to overwrite an unrelated Skill. Before running the doctor, stage only the portable files, review their complete staged diff, and commit them:

```bash
git status --short
git add .comeback-repository.json .agents/skills/release-safety/SKILL.md .gitignore
git diff --cached -- .comeback-repository.json .agents/skills/release-safety/SKILL.md .gitignore
git commit -m "Install Comeback repository policy"
```

The committed `.comeback-repository.json` is the stable repository identity. Comeback deliberately refuses activation when that anchor is missing, uncommitted, or differs from the copy at `HEAD`.

Comeback merges unrelated entries in an existing **untracked** hook configuration. It refuses a requested `.codex/hooks.json` or `.claude/settings.json` that Git already tracks, because the current launcher contains machine- and clone-absolute executable paths. The refusal happens before Comeback writes installation files; it does not silently untrack, move, or rewrite the tracked configuration.

For `--agent both`, Comeback validates both existing hook configurations and checks that any existing `release-safety` Skill belongs to Comeback before its first write. Invalid JSON or a conflicting Skill therefore refuses the whole installation instead of leaving only one agent partially configured.

If you deliberately choose to convert a tracked hook configuration into a clone-local file, preserve and review its contents first, run `git rm --cached -- PATH_TO_HOOK_FILE`, commit that deliberate repository-policy change, confirm the file remains in the working tree, and rerun `comeback init`. If the hook configuration must remain portable and tracked, leave it tracked and do not install the current Comeback integration for that agent in this clone. Run `comeback init` again in every new clone or after moving or reinstalling the uv tool; a generated hook file copied from another machine is not portable.

## Activate and prove Codex hooks

Installation does not prove activation. Codex ignores project-local hook configuration until both the repository and the exact hook commands are trusted.

1. Open interactive `codex` in the repository.
2. Choose **Yes, continue** when Codex asks whether to trust the directory.
3. Run `/hooks`.
4. Review the Comeback commands and choose **Trust all and continue**.
5. Exit Codex completely.
6. Run:

```text
comeback doctor --agent codex
```

The doctor consumes two authenticated Codex turns in two genuinely fresh, ephemeral processes and does not bypass hook trust. The first process is read-only and must create exactly one Sibyl run through the real `UserPromptSubmit` hook. The second uses a separate isolated Sibyl database and a disposable file under the ignored `.comeback/` directory; it must recall a seeded intervention, emit one exact `PreToolUse` denial, and leave the disposable release marker absent. `PASS` therefore proves activation and a real pre-execution block, not the full owner-approved release journey.

Never invoke `comeback-hook` yourself. It is a lifecycle protocol endpoint that expects structured JSON from the agent. Zero Sibyl runs means the hooks are inactive, regardless of whether installation or a direct launcher test succeeded. If `comeback status` shows `NO_AGENT_HOOK_RUNS`, do not attempt a release: run the doctor, fix the reported trust or installation issue, and start a new agent process.

See the [official Codex hooks documentation](https://developers.openai.com/codex/hooks) for the project-layer and hook-review model.

## Activate Claude Code hooks

Run `comeback init --agent claude`, review the generated `.claude/settings.json`, open Claude Code in the repository, and approve only the exact Comeback hooks you reviewed. Then exit Claude Code and run:

```text
comeback doctor --agent claude
```

Claude doctor intentionally returns `PARTIAL`: it proves the installed Git Bash launcher, lifecycle JSON, and Sibyl write, but it does not claim that a real Claude Code process dispatched the hook. The authenticated `scripts/run_cross_agent_gate.py` and `scripts/run_claude_unlock_gate.py` checks are separate and are required before making the cross-agent claim. Consequently, `comeback doctor --agent both` also remains `PARTIAL` when Codex passes and only the Claude launcher has been proven.

## Record the first intervention

Start a real Codex release task. If the agent skips a required check, stop it and get the exact session ID from:

```text
comeback status
```

Create the repository owner once:

```text
comeback create-owner
```

Run owner, signing, approval, and reconciliation commands yourself in a native terminal—not through the coding agent. `comeback create-owner` asks you to enter and confirm a new password. That password encrypts only `.comeback/owner-keystore.json`, the local key used to sign interventions, approvals, and reconciliations. It is not a Sibyl password, Codex password, funded wallet password, or Base transaction password. The generated address does not need ETH.

Prepare one intervention using commands that can execute directly without `&&`, pipes, redirection, or a shell interpreter. Store the prepared record inside ignored `.comeback/` so it does not make the checkpoint dirty. Use a direct HTTPS Git URL with no embedded username or token and an explicit source-to-destination refspec; do not sign a mutable remote name such as `origin`.

macOS or Linux example:

```bash
comeback prepare-intervention \
  --session-id CORRECTED_SESSION_ID \
  --summary "Agent skipped the release check" \
  --checkpoint-command ".uvenv/bin/python -m pytest -q" \
  --release-command "git push https://github.com/OWNER/REPOSITORY.git HEAD:refs/heads/main" \
  > .comeback/intervention.json
comeback intervene --record-file .comeback/intervention.json
```

PowerShell example, using JSON argument arrays so Windows paths and quoting are unambiguous:

```powershell
$python = (Resolve-Path .\.uvenv\Scripts\python.exe).Path
$checkpoint = @($python, "-m", "pytest", "-q") | ConvertTo-Json -Compress
$release = @("git", "push", "https://github.com/OWNER/REPOSITORY.git", "HEAD:refs/heads/main") | ConvertTo-Json -Compress
$record = comeback prepare-intervention `
  --session-id CORRECTED_SESSION_ID `
  --summary "Agent skipped the release check" `
  --checkpoint-argv-json $checkpoint `
  --release-argv-json $release
[IO.File]::WriteAllText(
  (Join-Path (Get-Location) ".comeback\intervention.json"),
  (($record -join "`n") + "`n"),
  [Text.UTF8Encoding]::new($false)
)
comeback intervene --record-file .comeback\intervention.json
```

Replace the interpreter path, repository URL, and destination branch with the real values before signing. Git may use the operating system's credential helper at execution time, but credentials must not appear in the signed URL or argument array.

`comeback intervene` prints the complete structured record to the terminal and requires you to type `SIGN` before it asks for the owner-keystore password. Read the repository, source session, agent scope, checkpoint arguments, release arguments, and authorized closer before confirming. External ERC-191 signers remain available through `--authorized-closer` and `--signature`, but they are an advanced path rather than an installation prerequisite.

New interventions default to `--agent-scope all_supported`, so a Codex correction can supervise Claude Code and vice versa. Use `--agent-scope same_agent` when appropriate. The scope, checkpoint command, release command, timeouts, repository identity, and authorized closer are all signed.

## Fresh supervised session

End the original agent process and start a genuinely fresh one with only the related release request. Comeback injects commands tied to that exact session:

```text
ABSOLUTE_COMEBACK_PATH --db ABSOLUTE_MEMORY_DB checkpoint --session-id FRESH_SESSION_ID
ABSOLUTE_COMEBACK_PATH --db ABSOLUTE_MEMORY_DB release --session-id FRESH_SESSION_ID
```

The actual injected commands contain the absolute path to the installed `comeback` or `comeback.exe`. Copy them exactly. Relative substitutes such as `comeback`, `./comeback`, extra flags, another session ID, or appended shell input are rejected by the hook.

The checkpoint capability resolves the signed executable once against the repository's captured PATH, fingerprints that absolute file, and executes that same absolute executable with the signed argument array and `shell=False` inside a managed process-tree boundary. With no operator override it uses the signed timeout; `--timeout` may only shorten that limit. Starting any recheck durably revokes the prior checkpoint receipt and human approval under a unique attempt nonce before the command can run. A failure, timeout, interruption, or overlapping/stale completion therefore cannot leave the older evidence authorized. A timeout or surviving background process is stopped and cannot mint a receipt. Windows `.bat` and `.cmd` launchers are refused because Windows may pass them through a command shell even with `shell=False`; use a native executable or an explicit Python/Node executable instead. A successful foreground exit records a receipt containing the repository fingerprint; model-reported output is never evidence. In `HUMAN_REQUIRED`, the developer then approves from a separate native terminal:

```text
comeback approve --session-id FRESH_SESSION_ID
```

`comeback approve` displays the session, mode, lesson IDs, checkpoint receipt digest, exact release arguments, repository-state policy, and remaining requirements. It proceeds only after you type `APPROVE` and enter the owner-keystore password. The signed approval is bound to that checkpoint receipt. At release preflight, Comeback refuses detected changes to the repository and the execution context it captures. For the supported direct Git-push form, it also replaces `HEAD` with the immutable checkpoint-approved commit ID. The release capability durably publishes a managed runner identity and Sibyl's `EXECUTING` state before it opens a one-shot start barrier, executes only the signed argument array, contains the process tree, and writes its observed process outcome directly to Sibyl.

## Unknown release outcomes and reconciliation

A timeout, process-start error, nonzero release exit, or failure to persist the final result is not proof that an external release did not partly succeed. Comeback records the outcome as `unknown`, raises supervision to `HUMAN_REQUIRED`, and retains the repository release lock so another session cannot retry blindly.

Inspect the real external target first. When the recorded release process is no longer running and you have determined what happened, reconcile from your native terminal with exactly one truthful resolution:

```text
comeback reconcile --session-id SESSION_ID --resolution released
comeback reconcile --session-id SESSION_ID --resolution not_released
```

Run only the line matching the verified external state. Comeback shows the prior status, reason, selected resolution, and warning; it requires you to type `RECONCILE` and enter the owner-keystore password. Until that signed reconciliation succeeds, the run remains unresolved and the release lock remains closed.

## Where Sibyl is load-bearing

Sibyl is the only store for repository-specific intervention lessons, signed action specifications, supervision runs, checkpoint receipts, approvals, and outcomes. The hook contains generic classification and enforcement mechanics but no copied intervention or current supervision mode.

Remove Sibyl state and the same fresh release request becomes `AUTONOMOUS`: its task-specific checkpoint and approval disappear. With memory enabled, the earlier intervention changes a fresh process from executing the raw release to denying it and requiring the remembered capabilities.

The main call sites are:

- intervention write: `src/comeback/memory.py`, `InterventionMemory.record_intervention`
- fresh-session recall: `src/comeback/memory.py`, `matching_lessons` and `start_run`
- checkpoint receipt and approval: `src/comeback/memory.py`, `record_checkpoint_receipt` and `approve`
- evolving outcome: `src/comeback/memory.py`, `record_release_outcome`
- action execution: `src/comeback/execution.py`
- lifecycle enforcement: `src/comeback/hook.py`, `handle`

## Security boundary

Comeback protects the configured release argument vector through its exact capability. Its raw-command detection is only defense in depth, not a general shell sandbox or complete command mediation layer. It recognizes direct release commands and common indirection, but a custom executable, unsupported tool, or another process can hide or perform an equivalent action. The capability is narrower: it receives no runtime command override and executes the preflight-resolved absolute executable with the signed argument array and `shell=False`. Windows batch launchers are not accepted. Never place passwords, private keys, API tokens, or credential-bearing URLs in a checkpoint or release argument array; use an operating-system credential helper or a future broker.

The checkpoint receipt correlates the signed checkpoint specification, repository/execution-context fingerprint, timestamps, session, and zero exit code. Its digest is an integrity and correlation checksum, not a signature, remote attestation, or independent proof that the check was semantically sufficient. The fingerprint covers Git-visible state, effective Git configuration and hooks, the resolved direct executable and direct file arguments, and selected environment variables at preflight. It does not freeze ignored files, inputs opened transitively by a custom executable, remote network responses, or changes made concurrently after the final preflight check. Only the direct Git-push capability pins its source artifact to an immutable approved commit. An attacker running as the same operating-system user who can replace the Sibyl database or call its local write API can forge or substitute receipt state.

For production, release credentials must be unavailable to the coding-agent process and exposed only through a separately authenticated Comeback-controlled broker. This local spike does not provide that credential boundary. An agent with unrestricted filesystem access can delete or replace the repository-local Sibyl database, release lock, owner keystore, or first-use repository/owner anchor. The committed repository anchor detects ordinary missing or changed anchor state, but it is not protection from an attacker who can alter both the working tree and trusted Git history. A future external trust registry can make owner identity and missing-state detection tamper-evident; it does not by itself isolate release credentials.

Do not use this spike to hold production deployment credentials or describe it as production security enforcement.

## Verify a development clone

With `uv`, the same setup works without a preinstalled Python.

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
uv venv .uvenv --python 3.13
uv pip install -e ".[dev]" --python .uvenv\Scripts\python.exe
.uvenv\Scripts\python.exe -m pytest -q
.uvenv\Scripts\python.exe scripts\run_validation_gate.py
.uvenv\Scripts\python.exe scripts\run_installed_hook_gate.py
.uvenv\Scripts\python.exe scripts\run_codex_hook_gate.py
```

The deterministic gate proves five fresh-session denials, a simulated Codex-to-Claude scope transition, malicious-prompt resistance, low-risk autonomy, signed checkpoint/approval/release, evolving supervision, and memory ablation. The installed-hook gate uses the generated POSIX command for Claude and the generated `commandWindows` through native PowerShell and `cmd.exe` for Codex. The real Codex gate proves a real Codex source session and a separate fresh Codex denial. Its setup then completes a signed `HUMAN_REQUIRED` capability run directly before a final fresh Codex process exercises the evolved `CHECKPOINTED` capability; it is activation and enforcement evidence, not one unbroken all-agent-driven approval journey.

The real Codex gate requires an authenticated local Codex CLI. Its explicit trust override and hook-trust bypass apply only to its newly created disposable repository; they are not the user onboarding path.

Run the real Claude gates separately only on a machine with authenticated Claude Code. They are not part of `comeback doctor --agent claude`:

macOS or Linux:

```bash
.uvenv/bin/python scripts/run_cross_agent_gate.py
.uvenv/bin/python scripts/run_claude_unlock_gate.py
```

Windows PowerShell:

```powershell
.uvenv\Scripts\python.exe scripts\run_cross_agent_gate.py
.uvenv\Scripts\python.exe scripts\run_claude_unlock_gate.py
```

The first authenticated Claude gate proves that a genuinely fresh Claude session recalls and blocks on an intervention whose Codex source run is seeded as a fixture. The second first seeds an owner-approved capability success directly, then proves a genuinely fresh Claude process recalls the evolved `CHECKPOINTED` mode, invokes its checkpoint and release capabilities, creates the side effect, and stores success. Neither script claims that its seeded Codex source was a real Codex process or that Claude performed the earlier owner approval.

`COMEBACK_MEMORY_DB` is an absolute-path-only diagnostic/development override. Both lifecycle hooks and normal CLI commands resolve it consistently, `comeback status` prints the authoritative selected database and whether an override is active, and hook-injected capability commands carry that exact database with `--db`. Leave the variable unset for the normal repository-local `.comeback/memory.db` journey. If a diagnostic intentionally exports it, use that same exported value—or the printed explicit `--db` path—for every operator-side `status`, `intervene`, `approve`, and `reconcile` command.

GitHub Actions runs the unit, deterministic memory, and installed-launcher gates on Linux and Windows. Authenticated real-agent gates remain release checks outside CI.

## Partner stacks

None in this spike. Base and Virtuals remain excluded until the real Codex and Claude journeys pass on their supported operating systems.

## Prior Work

See [PRIOR_WORK.md](PRIOR_WORK.md).
