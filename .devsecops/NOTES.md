---
id: overlay/NOTES
kind: overlay
summary: Invariants of the agent itself that a reviewer must respect.
---

# Product notes — ai-devsecops-agent

## Invariants

- Judgement never lives in this code. Severity criteria, quarantine rules and ecosystem procedures
  belong to the knowledge library; a threshold or a policy appearing in a module is a misplacement,
  not a feature.
- Planning, aggregation, deduplication, verdict computation and date arithmetic are deterministic.
  A change that makes any of them depend on a model is a defect even when its output looks right.
- Absence of a result is never success. Any path that turns a missing, invalid or unexecuted task
  into a passing run is a blocking defect.
- Configuration ships inside the `agent` package. A change that reads configuration from the
  repository root breaks installed use, where no repository exists.
- Values are read from structured files, never extracted from prose.

## Cautions

- Exit codes are an interface: CI acts on them. Changing one is a breaking change.
- The library is loaded by digest. Weakening or skipping that check silently allows a run on
  unverified knowledge.
