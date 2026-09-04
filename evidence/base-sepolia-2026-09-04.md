# Comeback Base Sepolia evidence — 2026-09-04

Evidence level: public onchain transactions plus founder-operated local verification. This document does not claim production security, user adoption, or a completed activation.

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

The claim transaction succeeded onchain and entered Base's `safe` chain view. Comeback then verified its sender, target, calldata, receipt, canonical block, deployed runtime, and inactive claimed state before writing the schema-2 `claimed` transition to `.comeback-repository.json`. Activation has not yet been sent, and no initial intervention identifier is represented as active onchain. Later evidence must record the separate safe-verified activation transaction before describing Base trust as active.

## Trust and RPC boundaries

The anchor is owner-specific trust on first use, not a globally canonical repository-ownership record. Because the key includes the repository ID, nonce, and owner, another wallet can create a parallel anchor. The committed repository configuration selects the expected one.

Comeback uses the official Base Sepolia endpoint by default. A custom non-loopback endpoint must use HTTPS, but remains a trusted single-provider override and can fabricate the view it returns. The client verifies chain ID, deployed runtime hash, exact transaction sender, target and calldata, successful receipt, canonical block identity, safe-head inclusion, and resulting contract state; those checks do not turn a dishonest RPC into independent consensus evidence.

The dedicated owner keystore and its password are not public evidence. The password encrypts the local private key; the same key may sign and send Base owner transactions and pay their testnet gas. No password or private key is stored in this repository.
