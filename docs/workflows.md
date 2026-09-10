# Corrections, approvals and workflow recovery

[Back to Comeback](../README.md) · [Installation](installation.md) · [Workflows](workflows.md) · [Base](base.md) · [Security](security.md) · [Validation](validation.md)

Complete [installation and hook activation](installation.md) before recording a correction. Keep the supervisor installation outside the repository it supervises.

## Record the first intervention

Start a real Codex release task. If the agent skips a required check, stop it and get the exact session ID from:

```text
comeback status
```

Create the repository owner once:

```text
comeback create-owner
```

Run owner, signing, approval, and reconciliation commands yourself in a native terminal—not through the coding agent. `comeback create-owner` asks you to enter and confirm a new password. That password encrypts only `.comeback/owner-keystore.json`, which holds the local owner key used to sign interventions, approvals, and reconciliations. When Base trust is enabled, the same owner key can also sign and send the repository's Base transactions, and its address must hold enough Base Sepolia ETH for those transactions. The password is not a Sibyl or Codex password, does not hold funds by itself, and is never sent to Base.

For guided correction capture, run this yourself in a native terminal after creating the owner:

```text
comeback capture
```

Choose the exact corrected session from the displayed recent release/migration sessions (or supply `--session-id`). Describe the missed check, explicitly choose one agent or both, then enter each executable and its arguments one per line. No shell quotes or handwritten JSON are needed, including for Windows paths with spaces. Blank input ends an argument list; `:cancel` aborts before signing. Review the signed scope and commands, type `SIGN`, then unlock your owner key. Capture does not execute either command, collect a chat transcript, or infer an unobserved command from a stored hash. Your incident description remains your report, not independently verified evidence. The guided path uses 600-second command timeouts; use the advanced path below for custom timeouts.

To understand a session's recorded requirements:

```text
comeback explain --session-id EXACT_SESSION_ID
```

This is a read-only explanation, not authorization: the release capability still checks evidence age, current repository state, and execution locks. A stale session requires a fresh working session. Guided capture reduces command/JSON preparation steps; human time savings have not yet been measured.

Alternatively, prepare one intervention using commands that can execute directly without `&&`, pipes, redirection, or a shell interpreter. Store the prepared record inside ignored `.comeback/` so it does not make the checkpoint dirty. For a real Git release, use a direct HTTPS URL with no embedded username or token. For credential-free local validation, use the absolute path to a disposable bare repository. In both cases use an explicit source-to-destination refspec; do not sign a mutable remote name such as `origin`.

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

Replace the interpreter path, release target, and destination branch with the real values before signing. Git may use the operating system's credential helper at execution time, but credentials must not appear in the signed URL or argument array.

`comeback intervene` prints the complete structured record to the terminal and requires you to type `SIGN` before it asks for the owner-keystore password. Read the repository, source session, agent scope, checkpoint arguments, release arguments, and authorized closer before confirming. External ERC-191 signers remain available through `--authorized-closer` and `--signature`, but they are an advanced path rather than an installation prerequisite.

New interventions default to `--agent-scope all_supported`, which makes a Codex correction eligible for Claude Code recall and vice versa. Treat that as a cross-agent claim only after the authenticated Claude gates pass on the installed version; the [recorded 3db9acc journey](https://youtu.be/jgZ2JFGiydE) uses Codex; the [versioned validation record](../evidence/agent-validation-2026-09-06.md) describes the narrower Claude fixtures. Use `--agent-scope same_agent` when appropriate. The scope, checkpoint command, release command, timeouts, repository identity, and authorized closer are all signed.

## Separate deployment and migration workflows

Comeback supports two signed workflow scopes: `release_workflow` for deployment and `migration_workflow` for a developer-defined database migration. Existing deployment records remain valid. Start a fresh agent session with an explicit request such as “Apply the database migration.” The source session's scope appears in the prepared intervention's signed `area` and deterministic lesson ID; review both before signing. Use the existing `--checkpoint-argv-json` and `--release-argv-json` options for your verifier and migration command. The CLI execution verb remains `release` for both scopes.

Migration and deployment lessons, receipts, approvals and outcome histories are separate. Success in one does not relax the other. Commands matching signed migration actions select that scope even if the prompt omitted migration wording. A session cannot switch protected workflows to reuse evidence: start a fresh session. Registering the identical protected argv in both scopes is refused; ambiguous raw command matches are denied. Execution remains serialized per repository for safety.

This is two bounded workflows, not arbitrary skill enforcement or a migration engine. Migration commands must enforce their own transactional preconditions: a source-code checkpoint does not bind a live database snapshot, and database changes do not automatically invalidate its receipt. Existing shell/credential isolation limits still apply. Base continues to anchor the repository owner and the initial intervention, not each later workflow's full history. Migration evidence includes deterministic SQLite/subprocess testing and a maintainer-operated real Codex migration/isolation run at an earlier revision; it is not independent user or authenticated cross-agent migration completion. See the [versioned validation record](../evidence/agent-validation-2026-09-06.md).

## Fresh supervised session

End the original agent process and start a genuinely fresh one with only the related release request. Comeback injects commands tied to that exact session:

```text
ABSOLUTE_COMEBACK_PATH --db ABSOLUTE_MEMORY_DB checkpoint --session-id FRESH_SESSION_ID
ABSOLUTE_COMEBACK_PATH --db ABSOLUTE_MEMORY_DB release --session-id FRESH_SESSION_ID
```

The actual injected commands contain the absolute installed executable path; on Windows they use the same installation environment's `python.exe -I -m comeback.cli`. Copy the injected command exactly. Relative substitutes such as `comeback`, `./comeback`, extra flags, another session ID, or appended shell input are rejected by the hook.

The checkpoint capability resolves the signed executable once against the repository's captured PATH, fingerprints that absolute file, and executes that same absolute executable with the signed argument array and `shell=False` inside a managed process-tree boundary. With no operator override it uses the signed timeout; `--timeout` may only shorten that limit. Starting any recheck durably revokes the prior checkpoint receipt and human approval under a unique attempt nonce before the command can run. A failure, timeout, interruption, or overlapping/stale completion therefore cannot leave the older evidence authorized. A detected timeout or surviving background process is stopped and cannot mint a receipt; [POSIX process-group containment does not cover a deliberately detached descendant](security.md#security-boundary). Windows `.bat` and `.cmd` launchers are refused because Windows may pass them through a command shell even with `shell=False`; use a native executable or an explicit Python/Node executable instead. A successful foreground exit records a receipt containing the repository fingerprint; model-reported output is never evidence. In `HUMAN_REQUIRED`, the developer then approves from a separate native terminal:

```text
comeback approve --session-id FRESH_SESSION_ID
```

`comeback approve` displays the session, mode, lesson IDs, checkpoint receipt digest, exact release arguments, repository-state policy, and remaining requirements. It proceeds only after you type `APPROVE` and enter the owner-keystore password. The signed approval is bound to that checkpoint receipt. At release preflight, Comeback refuses detected changes to the repository and the execution context it captures. For the supported direct Git-push form, it also replaces `HEAD` with the immutable checkpoint-approved commit ID. The release capability durably publishes a managed runner identity and Sibyl's `EXECUTING` state before it opens a one-shot start barrier, executes only the signed argument array, applies the [platform process-containment boundary](security.md#security-boundary), and writes its observed process outcome directly to Sibyl.

## Unknown release outcomes and reconciliation

A timeout, process-start error, nonzero release exit, or failure to persist the final result is not proof that an external release did not partly succeed. Comeback records the outcome as `unknown`, raises supervision to `HUMAN_REQUIRED`, and retains the repository release lock so another session cannot retry blindly.

Inspect the real external target first. When the recorded release process is no longer running and you have determined what happened, reconcile from your native terminal with exactly one truthful resolution:

```text
comeback reconcile --session-id SESSION_ID --resolution released
comeback reconcile --session-id SESSION_ID --resolution not_released
```

Run only the line matching the verified external state. Comeback shows the prior status, reason, selected resolution, and warning; it requires you to type `RECONCILE` and enter the owner-keystore password. Until that signed reconciliation succeeds, the run remains unresolved and the release lock remains closed.

## Upgrading older autonomy records

Remembered workflows never graduate out of their mandatory check. `AUTONOMOUS` means no matching intervention, not earned permission to skip verification. A new correction or confirmed failure resets the affected workflow to human review. Legacy lessons that earned `AUTONOMOUS` are interpreted as `CHECKPOINTED` without changing their signed intervention or historical runs; start a fresh agent session after upgrading, because old open autonomous runs cannot authorize a release under the new policy.
