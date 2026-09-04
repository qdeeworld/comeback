# Comeback Base Sepolia evidence — 2026-09-04

Evidence level: public onchain transactions plus founder-operated local verification. This document does not claim production security or user adoption.

## Bounded role

Base proves which wallet claimed the owner-specific anchor selected by the committed Comeback repository configuration and, after activation, which first Sibyl intervention that owner bound to it. The activation identifier is the SHA-256 digest of the exact domain-separated canonical intervention payload signed by that owner; Comeback verifies the corresponding Sibyl incident, signature, repository, owner, and projection membership before protected release work. Sibyl remains the sole adaptive memory for intervention content, task classification, requirements, approvals, runs, outcomes, and supervision-mode evolution.

Base does not store the adaptive policy, authorize every release, isolate credentials, or make arbitrary Sibyl changes by the same operating-system user tamper-proof. No Virtuals integration is claimed.

## Deployment

- Network: Base Sepolia, chain ID `84532`
- Registry: [`0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA`](https://sepolia.basescan.org/address/0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA)
- Deployment transaction: [`0xc8680aa5d09a20d9cb5afd3d24b665fcb71e2fc3a36729b93669e7b2afedf2c6`](https://sepolia.basescan.org/tx/0xc8680aa5d09a20d9cb5afd3d24b665fcb71e2fc3a36729b93669e7b2afedf2c6)
- Deployer and gas payer: `0xaB06eCBd04c5aF0540Efd730F27935Fc6fC9ADB7`
- Runtime bytecode hash: `0xa28c086af9980458acb83e005846259ea3cf3402320710d271188327d1922c81`
- Contract source commit: `4c19505a75df851da111a21cb3285ab670eb4a9f`
- CI for that commit: [GitHub Actions run 33902191505](https://github.com/qdeeworld/comeback/actions/runs/33902191505), including Foundry and Python jobs
- Source verification: [Sourcify exact match](https://repo.sourcify.dev/84532/0xe3C2D2A801904fa8c0d6C4456A6BEc853DfcFfDA)
- Local verification: the deployed runtime hash matched `forge inspect` output exactly, and the deployed `DOMAIN` matched `keccak256("COMEBACK_BASE_ANCHOR_V1")`

The immutable contract has no administrator, proxy, owner rotation, payable entry point, or external call. It exposes owner-specific claim, one-time initial-intervention activation, and read-only anchor lookup.

## Repository claim

- Repository ID: `9e9365d9456ae9aae5faeafa`
- Dedicated owner: `0x4fb0de16ab55cc33794c2c069e71b8c730de9966`
- Nonce: `0x219ce5b469057f9091b7b8c172c0ef9b965d5796e723a38332f60cd93b2d38a5`
- Derived anchor key: `0xece2ff579741214875c5d538ef377f354b950943df2c9ee712a4d58437f2be8b`
- Claim transaction: [`0x422a775ad53d75e73981613194aa15d6afefbf2148a6034fef0dc5edea4b3a44`](https://sepolia.basescan.org/tx/0x422a775ad53d75e73981613194aa15d6afefbf2148a6034fef0dc5edea4b3a44)
- Claim block: `46388566`
- Claim block hash: `0x732d80ddbdc0fb108382f45a58c7cdca309501e00eb6e26949e023713013cc55`

The claim transaction succeeded onchain and entered Base's `safe` chain view. Comeback then verified its sender, target, calldata, receipt, canonical block, deployed runtime, and inactive claimed state before writing the schema-2 `claimed` transition to `.comeback-repository.json`.

## Initial Sibyl intervention and activation

- Source harness: Codex
- Source session: `01a06dcc-aecd-7b30-9870-64727c79583e`
- Incident time: `2026-09-04T19:03:05.029891+00:00`
- Agent scope: all supported harnesses
- Initial mode: `HUMAN_REQUIRED`
- Required evidence: `release_check_passed`, then `human_approval`
- Intervention identifier: `4db3b1f830535c258a896ab30935da6addd2ed8c811d5a5e4338cba117e98b52`
- Owner signature: `1ce95e928f3ca410b353b95524187dadf26ba43851dd70707bfd640e51cafbb4062adb77d1f56d28245460185f88a257909801c40e4e9109e4efe73d4f0904811b`

The identifier is `sha256("Comeback intervention v1\n" + canonical_json(signed_fields))`. These are the complete signed fields, allowing the commitment and recovered owner to be independently checked:

```json
{
  "action_schema": 2,
  "agent_family": "Codex",
  "agent_scope": "all_supported",
  "area": "release_workflow",
  "authorized_closer": "0x4fb0de16ab55cc33794c2c069e71b8c730de9966",
  "checkpoint_spec": {
    "argv": [
      "/Users/qdee/Projects/comeback-spike/.venv/bin/python",
      "-m",
      "pytest",
      "-q"
    ],
    "timeout_seconds": 600
  },
  "incident_at": "2026-09-04T19:03:05.029891+00:00",
  "lesson_id": "release-release_workflow-codex",
  "release_spec": {
    "argv": [
      "git",
      "push",
      "/Users/qdee/Projects/comeback-spike/.comeback/local-release.git",
      "HEAD:refs/heads/approved"
    ],
    "timeout_seconds": 120
  },
  "repo_id": "9e9365d9456ae9aae5faeafa",
  "required_evidence": [
    "release_check_passed",
    "human_approval"
  ],
  "severity": "release_blocker",
  "source_session_id": "01a06dcc-aecd-7b30-9870-64727c79583e",
  "state_policy": {
    "bind_head": true,
    "require_clean_git": true
  },
  "task_class": "release"
}
```

Activation was sent only after [candidate CI run 33912626658](https://github.com/qdeeworld/comeback/actions/runs/33912626658) passed Foundry plus Ubuntu and native Windows on Python 3.12 and 3.13, including the repeated full suite, the fresh-session Sibyl replay, and installed platform-shell hooks.

- Activation transaction: [`0x0b578e80bff3f87aa02e8dd4a04cc2956ec35e60d2ddc0dba0eab409ee1b7003`](https://sepolia.basescan.org/tx/0x0b578e80bff3f87aa02e8dd4a04cc2956ec35e60d2ddc0dba0eab409ee1b7003)
- Activation block: `46391536`
- Activation block hash: `0x2867a6708e51eefef70730713ea911b7cb029e0a955c9bc14adb593393879f1b`
- Activation timestamp: `2026-09-04T19:49:20Z`
- Gas used: `53890`
- Safe head observed before the local transition: `46391722`

Comeback verified the activation sender, target, calldata, receipt, canonical block, safe-head inclusion, deployed runtime, exact anchored intervention and resulting contract state before writing the schema-2 `active` transition. The committed active configuration makes the initial Sibyl intervention mandatory: missing, substituted, corrupt, or incorrectly signed memory now blocks protected release work rather than returning to autonomous mode.

- Active configuration commit: `bb94ea1ccdbb6240c82b2a484781bd9c249f7450`
- CI for the active configuration: [GitHub Actions run 33913899687](https://github.com/qdeeworld/comeback/actions/runs/33913899687), with the Base contract and Ubuntu/native-Windows Python 3.12/3.13 jobs all passing
- Active-memory journey: [founder-operated ablation and autonomy-evolution evidence](active-memory-gate-2026-09-04.md)

## Trust and RPC boundaries

The anchor is owner-specific trust on first use, not a globally canonical repository-ownership record. Because the key includes the repository ID, nonce, and owner, another wallet can create a parallel anchor. The committed repository configuration selects the expected one.

Comeback uses the official Base Sepolia endpoint by default. A custom non-loopback endpoint must use HTTPS, but remains a trusted single-provider override and can fabricate the view it returns. The client verifies chain ID, deployed runtime hash, exact transaction sender, target and calldata, successful receipt, canonical block identity, safe-head inclusion, and resulting contract state; those checks do not turn a dishonest RPC into independent consensus evidence.

The dedicated owner keystore and its password are not public evidence. The password encrypts the local private key; the same key may sign and send Base owner transactions and pay their testnet gas. No password or private key is stored in this repository.
