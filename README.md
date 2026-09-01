# Comeback

[![Comeback validation](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml/badge.svg)](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml)

Comeback remembers where a coding agent needed human intervention, then changes how much autonomy the next agent receives for comparable work.

This repository is a bounded Sibyl hackathon validation spike, not a finished product. Its single scenario is a release-class task:

1. A developer signs an intervention after an agent attempted release before the required check.
2. Sibyl stores the intervention, provenance, task scope, outcome counts and current supervision mode.
3. The first process ends.
4. A genuinely fresh Codex session receives only a related release request.
5. A synchronous Codex `PreToolUse` hook recalls Sibyl and denies the release action.
6. A successful check plus the authorized closer's signed approval unlocks the action.
7. The release outcome updates the lesson; the next fresh session moves from `HUMAN_REQUIRED` to `CHECKPOINTED`.

Unrelated low-risk work remains `AUTONOMOUS`.

## Install in a repository

```bash
pipx install git+https://github.com/qdeeworld/comeback.git
cd your-repository
comeback init
```

`comeback init` merges Comeback into an existing `.codex/hooks.json`, installs the narrow release-safety Skill, and ignores the repository-local Sibyl database. It does not overwrite unrelated hooks or Skills. Open Codex in that repository, run `/hooks`, and trust the reviewed Comeback definition as required by Codex.

To turn a real human correction into enforceable memory:

```bash
comeback prepare-intervention \
  --session-id <corrected-codex-session> \
  --authorized-closer <ethereum-address> \
  --summary "Agent attempted release before the required check" \
  --checkpoint-command "pnpm test && pnpm run release:check" \
  > comeback-intervention.json
```

Sign the exact `message_to_sign` with the authorized address using an ERC-191-compatible wallet, then record it:

```bash
comeback intervene \
  --record-file comeback-intervention.json \
  --signature <wallet-signature>
```

The wallet never authorizes a transaction. Its signature prevents the coding agent, repository content, or another user from inventing or closing an intervention.

## Where Sibyl is load-bearing

Sibyl is the only store for repository-specific intervention lessons, supervision runs, required checkpoints, approvals and outcomes. The hooks contain generic task classification and enforcement mechanics but no copied intervention or mode.

Remove the Sibyl state and the same fresh release request becomes `AUTONOMOUS`; its task-specific checkpoint and human-approval requirement disappear. That materially breaks adaptive supervision.

The memory call sites are:

- intervention write: `src/comeback/memory.py`, `InterventionMemory.record_intervention`
- fresh-session recall: `src/comeback/memory.py`, `InterventionMemory.matching_lessons` and `start_run`
- evidence and approval: `src/comeback/memory.py`, `add_evidence` and `approve`
- evolving outcome: `src/comeback/memory.py`, `record_release_outcome`
- action enforcement: `src/comeback/hook.py`, `handle`

## Verify the spike

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/python scripts/run_validation_gate.py
.venv/bin/python scripts/run_codex_hook_gate.py
```

The first gate runs signed intervention, five fresh-session blocks, malicious-prompt resistance, low-risk autonomy, evidence, unauthorized and authorized approval, outcome evolution, and memory ablation. The second starts a real ephemeral Codex process and proves the project hook blocks before the release marker is created.

Every push also runs the deterministic fresh-session gate publicly in GitHub Actions and uploads its complete JSON replay artifact. The real Codex hook gate remains a local authenticated check because CI does not receive a Codex account credential.

## Partner stacks

None in this spike. Base and Virtuals are deliberately excluded until adaptive supervision passes the product gate.

## Prior Work

See [PRIOR_WORK.md](PRIOR_WORK.md).
