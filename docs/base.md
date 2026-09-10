# Base owner and initial-correction verification

[Back to Comeback](../README.md) · [Installation](installation.md) · [Workflows](workflows.md) · [Base](base.md) · [Security](security.md) · [Validation](validation.md)

An active Base anchor is checked before a managed checkpoint or release. Base verifies the selected owner and initial signed correction; Sibyl supplies the evolving supervision history. [The public demo](https://youtu.be/jgZ2JFGiydE) shows this verification in a separately labelled fixture using runtime `3db9acc`, including refusal when its anchored Sibyl correction is absent. It does not show a new transaction or production release.

## Optional Base Sepolia owner anchor

Comeback's bounded Base integration answers one question: which wallet claimed the owner-specific anchor selected by this repository and activated its first Sibyl intervention? It does not move the adaptive memory onchain.

Sibyl remains the only store for intervention content, task classification, signed action specifications, run history, checkpoint receipts, approvals, outcomes, and the evolving `HUMAN_REQUIRED`, `CHECKPOINTED`, and `AUTONOMOUS` modes. Base Sepolia stores only the selected owner address and, after activation, one initial Sibyl intervention identifier. That identifier is the SHA-256 digest of the exact domain-separated, canonical payload the owner signed, so it commits the initial checkpoint, release action, state policy, scope, provenance, and authority—not merely a session name. Activation refuses the older coordinates-only identifier format. If the committed configuration requires an active Base anchor but that exact incident is missing, corrupt, substituted, or has an invalid signature in Sibyl, Comeback fails the protected release closed. A Base or RPC failure also blocks a protected release; unrelated low-risk work does not require a Base call and remains available.

The deployed immutable registry is [`0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA`](https://sepolia.basescan.org/address/0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA) on Base Sepolia (chain ID `84532`). It has no administrator, proxy, owner rotation, payable entry point, or external call. Its deployment transaction is [`0xc8680aa5d09a20d9cb5afd3d24b665fcb71e2fc3a36729b93669e7b2afedf2c6`](https://sepolia.basescan.org/tx/0xc8680aa5d09a20d9cb5afd3d24b665fcb71e2fc3a36729b93669e7b2afedf2c6), and [Sourcify reports an exact source match](https://repo.sourcify.dev/84532/0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA). The expected runtime bytecode hash is `0xa28c086af9980458acb83e005846259ea3cf3402320710d271188327d1922c81`.

Enable the anchor only after the normal repository identity is committed. The least confusing path is to pass the schema-1 Codex doctor first and record exactly one owner-signed Sibyl intervention before claiming Base:

```text
# First complete normal hook activation and record exactly one Sibyl intervention.
comeback base-plan-claim
# Send exactly the returned zero-value Base Sepolia transaction from the displayed owner.
comeback base-claim --nonce NONCE --transaction CLAIM_TRANSACTION
# Review and commit the claimed .comeback-repository.json before continuing.

comeback doctor --agent codex
comeback base-plan-activation
# Send exactly the returned zero-value Base Sepolia transaction from the same owner.
comeback base-activate --transaction ACTIVATION_TRANSACTION
# Review and commit the active .comeback-repository.json.

comeback base-status
```

Claim-first setup is also accepted, but `comeback doctor` then reports `BASE_INTERVENTION_PENDING` until the one permitted initial intervention is present. The planning commands verify the configured Base deployment and print exact unsigned `to`, `data`, and `value_wei` fields; they never sign or broadcast. The claim and activation commands accept a transaction hash only after verifying its sender, target, calldata, receipt, canonical block, safe-head inclusion, deployed runtime, and resulting anchor state. Keep the password and decrypted key out of command output and repository files.

This is owner-specific trust on first use, not a global repository-ownership registry. The contract key includes the repository ID, nonce, and owner, so multiple wallets can create parallel anchors; the committed `.comeback-repository.json` selects the one this repository expects. The anchor detects substitution of that selected owner and, after activation, loss or alteration of the anchored initial Sibyl incident. It does not make arbitrary Sibyl state changes by the same operating-system user tamper-proof, attest that later outcome counters are truthful, isolate release credentials, or stop someone who can rewrite both the repository and its trusted Git history.

Comeback uses the official Base Sepolia endpoint by default. A custom `--rpc-url` must use HTTPS unless it is loopback, but it is still one trusted provider: a malicious or compromised endpoint could fabricate the chain view supplied to Comeback. Use independent chain evidence when that trust assumption is unacceptable.

An active Base anchor makes its onchain check part of every protected checkpoint and release preflight. Codex's `workspace-write` sandbox disables network access unless it is enabled explicitly, so start the fresh supervised Codex process with:

```bash
codex --strict-config -c 'sandbox_workspace_write.network_access=true' --sandbox workspace-write
```

This grants network access to the agent sandbox; it does not bypass Comeback hooks or Codex approvals. Review that tradeoff before using it. If the Base RPC is unavailable or network access remains disabled, Comeback refuses the protected capability without minting a checkpoint receipt or executing the release. After changing this setting, start a genuinely fresh Codex session rather than reusing the failed one. Low-risk work does not require this Base preflight.

## Recorded chain evidence

See [deployment, claim and activation receipts](../evidence/base-sepolia-2026-09-04.md) and the [earlier active-memory journey](../evidence/active-memory-gate-2026-09-04.md). They identify their exact revisions and limitations. Base is the only partner stack used; there is no Virtuals integration.
