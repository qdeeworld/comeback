# Codex resume: execution-environment regression

September 10, 2026. macOS; Python 3.14.6; Codex CLI 0.153.4.

Scope: the implementation and tests shipped alongside this record. This is an
authenticated execution regression using a disposable repository and generated
test owner, not a cold-user test or a complete lifecycle-hook onboarding test.

## Observed result

`COMEBACK_TEST_AUTHENTICATED_CODEX=1 python -m pytest tests/test_codex_resume_execution.py -q -s`
passed (1 test, 59.26 seconds). Real Codex thread:
`01a08ba1-95e9-75b0-8349-bbf0509edad9`.

- The first CLI process invoked the exact checkpoint capability and exited.
- The test owner signed approval for that receipt outside the agent.
- A new CLI process resumed the same Codex thread and invoked the release capability.
- Checkpoint and release ran in distinct child processes, 54561 and 56979.
- Raw PATH hashes differed; the effective capability PATH hash was identical.
- The original checkpoint receipt digest and signed approval remained unchanged.
- The release returned `release_completed`, exit 0, outcome `success`; the previously
  absent disposable `released.txt` marker was created and verified.

Effective PATH SHA-256 in both processes:
`4388ffd35d8ac50a6741c234a2133da86976240112d108b3b7cf2373b01f0c8c`.
Environment values and signing secrets are not included.

## Security and regression coverage

Only per-launch Codex shim PATH entries under the default/configured Codex home
are excluded. They are removed from the actual child execution environment as
well as its fingerprint. Ordinary PATH entries and ordering remain significant;
changed captured tokens or Git configuration still invalidate approved evidence.
The tests verify that a fake Git executable in an excluded shim is not used, the
caller environment is not mutated, and a shim-only PATH fails closed rather than
accidentally enabling current-directory executable search.

Targeted execution suite: 58 passed, 2 platform skips. Full local suite: 512 passed,
8 skips, including the opt-in authenticated test skipped in the default suite.
The release-check wrapper passed the full suite again. Deterministic memory gate:
GREEN. Installed-hook shell gate on macOS: PASS for Codex and Claude command forms.
These local results are not native Windows or authenticated Claude execution.

The environment policy is versioned in the fingerprint. Older receipts must be
refreshed after upgrading; no old signature is silently reinterpreted. The receipt
still expires after 15 minutes and is still tied to its supervision session.
This change does not fix arbitrary shell sandboxing or the existing same-OS-user
memory-tampering limitations described in the README.
