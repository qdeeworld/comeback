# Comeback

[![Comeback validation](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml/badge.svg)](https://github.com/qdeeworld/comeback/actions/workflows/validation.yml)

Comeback remembers where a coding agent needed human intervention, then changes how much autonomy the next agent receives for comparable work.

This repository is a bounded Sibyl hackathon validation spike, not a finished product. Its single scenario is a release-class task:

1. A developer signs an intervention after an agent attempted release before the required check.
2. Sibyl stores the intervention, provenance, task scope, outcome counts and current supervision mode.
3. The first process ends.
4. A genuinely fresh Codex or Claude Code session receives only a related release request.
5. A synchronous `PreToolUse` hook recalls Sibyl and denies the release action—even when the intervention came from the other agent.
6. A successful check plus the authorized closer's signed approval unlocks the action.
7. The release outcome updates the lesson; the next fresh session moves from `HUMAN_REQUIRED` to `CHECKPOINTED`.

Unrelated low-risk work remains `AUTONOMOUS`.

## Install in a repository

```bash
pipx install git+https://github.com/qdeeworld/comeback.git
cd your-repository
comeback init
```

`comeback init` merges Comeback into existing `.codex/hooks.json` and `.claude/settings.json` files, installs the narrow release-safety Skill, and ignores the repository-local Sibyl database. It does not overwrite unrelated hooks or Skills. Review and approve the project hooks when Codex or Claude Code asks.

If `pipx` is unavailable, install with `uv`:

```bash
uv tool install git+https://github.com/qdeeworld/comeback.git
cd your-repository
comeback init
```

For development from a clone on a host without `python3-venv` or `ensurepip`:

```bash
uv venv .uvenv
uv pip install -e '.[dev]' --python .uvenv/bin/python
.uvenv/bin/pytest -q
.uvenv/bin/python scripts/run_validation_gate.py
```

Always run `comeback init` with the installed environment's `comeback` executable. The installer writes the resolved `comeback-hook` path into both agent configurations; the repository does not assume a `.venv` directory name.

To turn a real human correction into enforceable memory:

```bash
comeback prepare-intervention \
  --latest \
  --authorized-closer <ethereum-address> \
  --summary "Agent attempted release before the required check" \
  --checkpoint-command "pnpm test && pnpm run release:check" \
  > comeback-intervention.json
```

`comeback status` shows recent Sibyl-backed runs and their session IDs. `--latest` binds the intervention to the most recently updated run; use `--session-id` when selecting an older correction.

New interventions default to `--agent-scope all_supported`, so a correction created after a Codex incident can supervise Claude Code and vice versa. Use `--agent-scope same_agent` when the lesson should remain specific to the source harness. The selected scope is part of the signed intervention and cannot be widened later without a new signature.

Sign the exact `message_to_sign` with the authorized address using an ERC-191-compatible wallet, then record it:

```bash
signature=$(cast wallet sign --interactive "$(jq -r .message_to_sign comeback-intervention.json)")
comeback intervene \
  --record-file comeback-intervention.json \
  --signature "$signature"
```

The wallet never authorizes a transaction. The first signed intervention is a local trust-on-first-use anchor for that lesson. Later records cannot replace its authorized closer, and only that closer can approve the supervised action. A future external trust registry is required to protect the first anchor from an agent that can replace the entire local store.

In the fresh supervised session, run the exact checkpoint invocation Comeback recalls. New interventions bind a random signed success marker to that invocation, so Codex's stdout-only hook response records evidence only when the shell reaches the marker after a zero exit. For `HUMAN_REQUIRED`, prepare and sign the approval from a separate terminal:

```bash
comeback prepare-approval --latest > comeback-approval.json
approval_signature=$(cast wallet sign --interactive "$(jq -r .message_to_sign comeback-approval.json)")
comeback approve \
  --session-id "$(jq -r .session_id comeback-approval.json)" \
  --approved-at "$(jq -r .approved_at comeback-approval.json)" \
  --signature "$approval_signature"
```

`cast wallet sign --interactive` keeps the private key out of shell history. A Ledger, Trezor, encrypted Foundry keystore, AWS KMS, GCP KMS, Turnkey, or another ERC-191 wallet can sign the same message instead. Never pass a raw production private key on the command line.

## Where Sibyl is load-bearing

Sibyl is the only store for repository-specific intervention lessons, supervision runs, required checkpoints, approvals and outcomes. The hooks contain generic task classification and enforcement mechanics but no copied intervention or mode.

Remove the Sibyl state and the same fresh release request becomes `AUTONOMOUS`; its task-specific checkpoint and human-approval requirement disappear. That materially breaks adaptive supervision.

Comeback currently recognizes a bounded set of direct release commands and common spellings such as `command git push`, absolute executable paths, and package-manager deploy scripts. It is not a general shell sandbox: arbitrary aliases or custom wrappers can hide an equivalent deployment from a syntactic hook. Real release authority therefore requires credentials to be exposed only through a protected capability, not broadly inside the agent process.

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
.venv/bin/python scripts/run_installed_hook_gate.py
.venv/bin/python scripts/run_codex_hook_gate.py
.venv/bin/python scripts/run_cross_agent_gate.py
.venv/bin/python scripts/run_claude_unlock_gate.py
```

The first gate runs signed intervention, five fresh-session blocks, Codex-to-Claude recall, malicious-prompt resistance, low-risk autonomy, evidence, unauthorized and authorized approval, outcome evolution, and Claude-side memory ablation. The installed-hook gate executes the generated Codex and Claude commands through Bash, including Git Bash on Windows CI. The real Codex gate starts two ephemeral processes: one proves the hook blocks before the release side effect, and the next proves the complete checkpoint → authorized release → successful outcome loop using exit-bound markers. The remaining gates prove a Codex intervention blocks a real fresh Claude Code process and then prove Claude's complete checkpoint → release → outcome loop. The Claude gates require an authenticated local Claude Code installation.

The gate scripts resolve Windows `.exe` console scripts, quote their paths for Claude Code's Git Bash hook shell, use structured Claude permission-denial evidence, and tolerate Sibyl's open SQLite handle during Windows temporary-directory cleanup. Installed Claude hooks pass the agent family as an explicit argument instead of relying on POSIX-only environment-variable syntax.

Every push runs the unit suite, deterministic fresh-session gate, and installed Bash-hook gate on Linux and Windows in GitHub Actions, then uploads the JSON replay artifacts. The real Codex and Claude Code agent gates remain local authenticated checks because CI does not receive either account credential.

## Partner stacks

None in this spike. Base and Virtuals are deliberately excluded until adaptive supervision passes the product gate.

## Prior Work

See [PRIOR_WORK.md](PRIOR_WORK.md).
