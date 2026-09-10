# Comeback

[![Comeback validation](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml/badge.svg)](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml)

**Carry a developer-approved correction into the next coding-agent session. Keep the check mandatory; reduce repeat approval.**

Comeback uses Sibyl Memory to remember the exact check and protected action you approve for a repository workflow. Fresh Codex or Claude Code sessions recall that requirement. After successful supervised execution, later matching sessions still run the check but no longer require the same owner approval.

[Watch the 2:38 demo](https://youtu.be/jgZ2JFGiydE) · [Setup and troubleshooting](docs/installation.md) · [Workflow guide](docs/workflows.md) · [Validation](docs/validation.md)

The demo uses Comeback runtime [`3db9acc`](https://github.com/qdeeworld/comeback/tree/3db9acc9efdd159d881fe903f9a354b6d99281c7) and Codex CLI `0.153.4`. It turns a real code-review regression into an owner-approved requirement, uses a disposable local Git destination, and shows Base verification in a separate labelled fixture. It is not evidence of an organically skipped Skill, a production release or a new Base transaction.

## What it does

1. You identify a correction and sign its check, protected command and scope.
2. Sibyl stores the signed requirement, provenance and subsequent execution history.
3. A fresh matching session recalls `HUMAN_REQUIRED`: check first, then owner approval.
4. After that intervention exists, the hook denies recognized raw release commands for its workflow. The configured action runs through its exact Comeback capability only after its requirements pass.
5. Successful capability execution moves that workflow to `CHECKPOINTED`: **the check stays; repeat approval goes away**.

No matching intervention means `AUTONOMOUS`, not permission to ignore ordinary repository rules. A new correction or confirmed failure restores human review; unresolved execution requires owner reconciliation.

There are two configured scopes: deployment/release and developer-defined database migration. Their histories are separate, but both use the CLI's `release` capability. This is not automatic enforcement of arbitrary Skills or a migration engine. [Workflow scope and migration limits](docs/workflows.md#separate-deployment-and-migration-workflows).

## Quick start

Start with a disposable, normal Git working tree with at least one commit. Linked Git worktrees are currently refused. Install Comeback separately from the repository it supervises.

You need Git, an authenticated supported coding-agent CLI, and [`uv`](https://docs.astral.sh/uv/getting-started/installation/). `uv` supplies Python; no separate `pipx` or Python installation is required. Claude on Windows also needs Git Bash. Check [versioned results](docs/validation.md#versioned-results): an older authenticated run does not certify every newer agent or Comeback revision.

### 1. Install

macOS or Linux — install `uv` from the linked instructions first:

```bash
uv python install 3.13
uv tool install --python 3.13 "git+https://github.com/qdeeworld/comeback.git"
uv tool update-shell
# Close and reopen the terminal so comeback is on PATH.
cd /path/to/your-repository
comeback init --agent codex
```

Windows PowerShell:

```powershell
winget install --id=astral-sh.uv -e --source winget --scope user
# Close and reopen PowerShell so uv is on PATH.
uv python install 3.13
$env:UV_LINK_MODE = "copy"
uv tool install --python 3.13 "git+https://github.com/qdeeworld/comeback.git"
uv tool update-shell
# Close and reopen PowerShell so comeback is on PATH.
cd C:\path\to\your-repository
comeback init --agent codex
```

Use `--agent claude` or `--agent both` when appropriate. If Winget is unavailable, use the other official `uv` installation methods. Keep Windows copy mode enabled **before** installation. For blocked executables, missing metadata or Git ownership errors, use [platform troubleshooting](docs/installation.md); do not disable security or repeatedly reinstall.

### 2. Review the repository files and hooks

Initialization writes portable policy plus machine-local hooks. Review and commit only the portable files:

```bash
git status --short
git add .comeback-repository.json .agents/skills/release-safety/SKILL.md .gitignore
git diff --cached -- .comeback-repository.json .agents/skills/release-safety/SKILL.md .gitignore
git commit -m "Install Comeback repository policy"
```

Review, but do not commit, `.codex/hooks.json` / `.claude/settings.json`: their launcher paths belong to this installation. Existing tracked hook files are refused; [conversion and relocation guidance](docs/installation.md#install-on-macos-or-linux) explains the deliberate choices. A missing or uncommitted repository identity also refuses activation.

**Codex:** open `codex`, trust the reviewed directory, enter `/hooks`, review and trust the Comeback hooks, then exit completely. Run:

```text
comeback doctor --agent codex
```

Require `PASS`. Doctor uses two authenticated turns to prove activation, sandbox Git access and a seeded pre-tool denial—not a complete approved release. Its stores are isolated, so `NO_WORKING_AGENT_RUNS` afterward is expected until a fresh working session starts. Never invoke `comeback-hook` manually.

**Claude Code:** review the generated settings and hooks, open Claude in the repository, approve those exact hooks, then exit and run `comeback doctor --agent claude`. Its expected `PARTIAL` result verifies the launcher, not authenticated lifecycle dispatch. Use the separate [real-agent validation checks and their limits](docs/validation.md) before relying on a cross-agent claim.

### 3. Capture your correction

Start a real working agent session for a release or migration task. When you need to correct its requirement, stop it and run the following yourself in a native terminal:

```text
comeback status
comeback create-owner
comeback capture --session-id EXACT_CORRECTED_SESSION_ID
```

Create the owner only once. Its password encrypts the local signing key in `.comeback/owner-keystore.json`; it is not a Sibyl, Codex or Claude password. Keep the password and signing commands outside the agent.

Capture asks for your description, `same_agent` or `all_supported` scope, then the check and protected action: **one executable and one argument per line, without shell quotes**. A blank line ends the arguments; `:cancel` aborts. Review the complete scope, type `SIGN`, and unlock your key. Your description remains an owner report, not independently verified evidence of a mistake.

For Git push, supply a direct credential-free HTTPS URL or absolute bare-repository path and an explicit refspec such as `HEAD:refs/heads/approved`, not `origin`. A [disposable local target](docs/installation.md#optional-credential-free-windows-release-target) needs no GitHub account. Never put tokens or passwords in signed arguments. [Advanced capture, signing and timeouts](docs/workflows.md#record-the-first-intervention).

### 4. Complete the next supervised task

End the source agent process, then start a genuinely fresh session with the related task. The hooks supply the exact session-bound checkpoint and release commands. Let the agent use those commands unchanged; do not substitute a path, session ID, database or extra shell input.

After the checkpoint passes, inspect and approve from your separate native terminal when `HUMAN_REQUIRED` requires it:

```text
comeback explain --session-id FRESH_SESSION_ID
comeback approve --session-id FRESH_SESSION_ID
```

Review the receipt and destination, type `APPROVE`, and enter your owner-key password. Return to that same agent session to execute the release capability. Approval is bound to the checkpoint, session and captured repository/execution state; it expires with the receipt. A recheck revokes older check/approval evidence. [Full execution and restart rules](docs/workflows.md#fresh-supervised-session).

On the next matching session, successful history removes repeat approval, **not** the mandatory check. A successful process does not necessarily mean a new deployment: an up-to-date Git push may do no new work.

If an outcome is `unknown`, **do not retry**. Inspect the real destination and follow [owner reconciliation](docs/workflows.md#unknown-release-outcomes-and-reconciliation). Changing environment or repository state can require a new checkpoint and approval.

## Where Sibyl is load-bearing

Sibyl is the only store for workflow-specific signed interventions, supervision runs, checkpoint receipts, approvals and outcomes. The hook contains generic enforcement logic, not another copy of those requirements.

A fresh process matches repository identity, workflow and agent scope to the stored lesson. That recalled state changes the required check and whether owner approval is needed. Successful history changes future supervision without removing verification.

Delete Sibyl without an active Base anchor and the learned workflow requirements disappear. With an active Base anchor, missing initial memory instead refuses protected execution. In either case, Comeback loses its adaptive supervision; Base cannot reconstruct the missing history.

| Operation | Source |
| --- | --- |
| Write a correction | [memory.py](src/comeback/memory.py), `InterventionMemory.record_intervention` |
| Recall in a fresh session | [memory.py](src/comeback/memory.py), `matching_lessons`, `start_run` |
| Store check and approval | [memory.py](src/comeback/memory.py), `record_checkpoint_receipt`, `approve` |
| Update supervision from outcomes | [memory.py](src/comeback/memory.py), `record_release_outcome` |
| Gate tools / execute the configured action | [hook.py](src/comeback/hook.py), `handle`; [execution.py](src/comeback/execution.py) |

Memory makes the previously approved correction and its outcome history available to the next session—not merely a longer prompt. Superiority over a competent static gate and net developer time savings are not yet established.

## Partner stacks

**Base Sepolia verifies the selected repository owner and its initial signed correction before managed checkpoint/release execution.** Once activated, its live contract check requires the anchored incident to remain valid in Sibyl. Missing required memory or unavailable Base verification refuses the protected action.

Registry: [`0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA`](https://sepolia.basescan.org/address/0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA), chain `84532`. The [Base setup guide](docs/base.md) includes activation commands, network requirements and trust assumptions; [public chain evidence](evidence/base-sepolia-2026-09-04.md) includes deployment, claim and activation receipts.

Sibyl still stores evolving requirements and outcomes. Base is not per-release approval, a payments feature, credential isolation or a global ownership registry. The demo's separate Base fixture shows a live verification and missing-memory refusal, not a new transaction. **No Virtuals integration is claimed.**

## Security boundary

This is configured local workflow supervision, not a sandbox for an unrestricted hostile agent. Raw-command recognition is defense in depth; the signed capability is narrower. The same operating-system user can tamper with local state or the installation. A passed check proves command completion under captured conditions, not semantic correctness or a live database snapshot.

Keep production deployment credentials out of this prototype. Read the [full trust and execution boundaries](docs/security.md) before use, including editable installations, process containment, stale evidence and single-provider Base RPC trust. Known limitations and ongoing hardening are not an all-fixed or production-ready claim.

## Verify a development clone

[Validation instructions](docs/validation.md) contain complete macOS/Linux and Windows commands for unit tests, deterministic memory checks, installed hooks and authenticated agent runs. CI does not run authenticated coding agents.

Use [versioned results](docs/validation.md#versioned-results) to distinguish public recordings, earlier real-agent checks, seeded fixtures and unassisted user evidence. Do not transfer an older PASS to a changed runtime or agent version.

## Prior Work

Comeback was created on September 1, 2026. It reused the Sibyl local-SQLite/separate-process pattern and Base transaction-verification experience from the separate PoolDeal spike, plus an existing funded test wallet for gas. No PoolDeal product code, UI, contracts, brand or repository history was copied. The Comeback integrations, memory model and Base anchor were built after kickoff. Wallet activity is infrastructure, not adoption.

The complete [Prior Work declaration](PRIOR_WORK.md) is retained. Licensed under [MIT](LICENSE).
