# 0008. Import inside functions where it breaks a cycle or defers a cost

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Ruff's PLC0415 (`import-outside-top-level`) would flag ~106 sites. Two distinct
reasons put them there, and neither is an oversight.

Optional extras -- pydicom, pynetdicom, keyring -- must not be imported unless
used, because they are genuinely absent from a default install. Importing them
at module level turns `xnatctl --help` into an ImportError for anyone without
the extra.

The rest break import cycles between the CLI, service and core layers.

## Decision

Lazy imports stay, and **PLC0415 is not enabled**. The reason is recorded next
to the ruff `select` list in `pyproject.toml` so it is not "cleaned up" later.

Each lazy import should be obvious about which reason applies -- an optional
extra reads as such; a cycle-breaker deserves a comment.

## Consequences

- An import error for an optional extra surfaces when the command runs, not at
  startup. The affected commands catch it and print an actionable message
  naming the extra to install.
- The cycle-breaking cases are papering over layering that the architecture
  track intends to fix. When that lands, those specific imports should move
  back to module level -- the optional-extra ones should not.
- Nothing mechanically enforces the distinction. That is the cost of leaving
  the rule off.

## Alternatives considered

- **Enable PLC0415 with per-line noqa.** Rejected: ~106 markers documenting a
  deliberate convention is noise, and the ones for optional extras would never
  be removed.
