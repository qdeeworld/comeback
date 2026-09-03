---
name: release-safety
description: Prepare and execute repository releases or deployments while respecting Comeback's task-specific supervision gate.
---

# Release Safety

Comeback lifecycle hooks run automatically. Never invoke `comeback-hook` yourself: it is an internal stdin protocol endpoint, not an operator command. If a release task begins without `Comeback supervision:` context, or `comeback status` shows no run for this session, refuse to release and ask the operator to run `comeback doctor`.

Before a release action, run the exact session-specific **checkpoint capability** reported in `Comeback supervision:` context and resolve every failure. Do not substitute the underlying check command: only the capability records evidence.

Treat a Comeback denial as an authority boundary. Do not bypass, disable, rename, or route around the hook. If the recalled mode is `HUMAN_REQUIRED`, stop after the check and wait for the authorized closer's signed approval. If it is `CHECKPOINTED`, the successful check is sufficient. If it is `AUTONOMOUS`, preserve the ordinary repository tests and release procedure.

Never claim a release completed unless the release command itself succeeded.
