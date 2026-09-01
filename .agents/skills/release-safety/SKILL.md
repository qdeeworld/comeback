---
name: release-safety
description: Prepare and execute repository releases or deployments while respecting Comeback's task-specific supervision gate.
---

# Release Safety

Before a release action, run the exact remembered checkpoint command reported by Comeback and resolve every failure.

Treat a Comeback denial as an authority boundary. Do not bypass, disable, rename, or route around the hook. If the recalled mode is `HUMAN_REQUIRED`, stop after the check and wait for the authorized closer's signed approval. If it is `CHECKPOINTED`, the successful check is sufficient. If it is `AUTONOMOUS`, preserve the ordinary repository tests and release procedure.

Never claim a release completed unless the release command itself succeeded.
