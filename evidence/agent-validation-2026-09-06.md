# Agent validation — September 6, 2026

Tested release: `d4a38d3d19ce8a60d05fe5056e51e2dae0ef7cd2`.
This is a maintainer summary of execution evidence, not a claim of production
security, customer adoption, or compatibility with every agent version.
Original external reports and local transcripts are retained privately; the
summary below does not make those raw artifacts publicly accessible.

## Results and scope

| Environment | Observed result | Important limit |
| --- | --- | --- |
| macOS, Codex CLI 0.153.3 | Separate real processes performed the source action, fresh-session denial, checkpoint plus test-owner-approved release, then a later CHECKPOINTED checkpoint/release | Maintainer-operated disposable fixture; approval signed by a separate test-owner process, not an independent human |
| Windows, Python 3.13.13, Claude Code 2.1.263 | External tester's fresh Claude session recalled HUMAN_REQUIRED and denied the raw release before its side effect | Codex source intervention seeded by the test harness, not a live Codex-to-Claude handoff |
| Same Windows Claude environment | Separate fresh Claude session completed checkpoint then release, both exit zero; success persisted; zero retries or permission denials | CHECKPOINTED mode established by direct setup before Claude started; not live Claude owner approval or evolution |
| Windows and Ubuntu, Python 3.12/3.13 | [Exact-release CI passed](https://github.com/qdeeworld/comeback/actions/runs/34023640810), including memory and installed-shell gates | CI does not run authenticated agents |

These results establish the bounded release behaviors on the named versions.
They do not establish production credential isolation, an arbitrary Skill
enforcer, or an unassisted cold-user onboarding completion.

## Real Codex release sequence

The merged-release check used a private wrapper around the public Codex gate to
replace its direct setup completion with actual Codex capability execution.
The owner signer stayed outside the agent and approved the exact session only
after its checkpoint receipt existed.

- Source: `01a07608-3054-7db1-ad7f-f16c7c4931bb` executed the disposable action.
- Fresh denial: `01a07608-98c7-7170-9ede-78e44e70790e` recalled HUMAN_REQUIRED;
  the raw action was denied before the release marker existed.
- Approved execution: `01a07609-38e8-71d3-b49c-36f621f02314` completed a real
  checkpoint and release with test-owner approval; success evolved the lesson.
- Later fresh session: `01a0760a-17ce-7e91-8dcf-35f9c9d4d1a2` recalled
  CHECKPOINTED and completed checkpoint/release without new owner approval.

The source action was explicitly requested for validation, not evidence that an
agent naturally ignored a developer's instruction. A preceding private-wrapper
attempt failed while inspecting an already-completed run with a stale-revision
check; its failed report was preserved. Correcting that historical-result read
changed no production code. The final sequence above passed separately.

The public `scripts/run_codex_hook_gate.py` has a narrower setup: it directly
completes the intermediate owner-approved run. Do not describe that script as
reproducing the private wrapper's entire all-agent execution sequence.

## External Claude release checks

The external report supplies the exact Git head and exit zero for both
`scripts/run_cross_agent_gate.py` and `scripts/run_claude_unlock_gate.py`.

- Denial session: `9fe3cac8-0cdd-4dfc-af79-0ae3f62f61c8`.
  The reported checks confirm lifecycle dispatch, HUMAN_REQUIRED recall,
  release-tool denial, and an absent release side effect.
- Execution session: `81beec98-6955-4a0b-b52a-89c22cbd13fe`.
  The supplied raw Claude stream contains exactly the checkpoint and release
  capability calls, successful tool results, and matching Sibyl permission
  records. The checkpoint recorded its receipt; release stored `success`.
  `retry_count` is zero and `capability_retry_requires_review` is false.

This supersedes the earlier readiness-error/retry result for these tested
behaviors; the older strict FAIL remains a historical failure. The successful
run does not prove which exception occurred on that earlier host invocation.

Original artifact SHA-256 checksums (integrity references, not public downloads):

| Artifact | SHA-256 |
| --- | --- |
| final-head.txt | `1f67ab707e25a1b7a9794bc9a119890fb4adabb052ecddf57a51f03af196c697` |
| final-versions.txt | `3522d236004c8798997d275d63f193df3abf9207ac68b110efca5e8c7ffb042e` |
| final-exit-codes.txt | `d024bc91f1e80638c42a90ea08f2c0cab8fc83e48dc1b7a579a4eb13968521ec` |
| final-cross-agent.json | `a1aed2c278ed845e6219e886d8632ff546c47c4d74a1a53098820add3c00dec3` |
| final-claude-unlock.json | `771952f254e676973ace996c4bf42adbda2f8117b29270b0769be473f7ea9b26` |

## Migration and Base boundaries

A separate maintainer-operated Codex 0.153.3 run at
`dfdb630ffb147ed85bf8c024c4d244bfb5f7d1f0` exercised a local SQLite migration:
fresh-session denial, checkpoint, test-owner approval, index creation, stored
success, and migration-only evolution. A separate deployment obligation remained
HUMAN_REQUIRED and its action was denied. This was not rerun as a real migration
journey on d4a38d3; current CI includes workflow-isolation regression tests.

The migration source agent actually ran its check voluntarily. The initial
fixture's skipped-check description was inaccurate and is disqualified as
evidence of a genuine developer correction. The valid result is enforcement of
an owner-authored test obligation and isolation between two workflow histories.
The deployment obligation was seeded. No live database snapshot safety follows.

None of the agent runs described here executed a Base transaction. Base's
separate [active-memory validation](active-memory-gate-2026-09-04.md) covers the
owner/initial-intervention anchor. It does not move evolving memory onchain or
remove the local same-user and credential limitations in the README.

For reproducible setup and the exact scope of each public gate, use
[Verify a development clone](../README.md#verify-a-development-clone).
