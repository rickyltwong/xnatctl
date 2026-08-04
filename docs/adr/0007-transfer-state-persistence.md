# 0007. Persist cross-server transfer state in SQLite

- **Status:** Accepted
- **Date:** 2026-08-04 (recorded retrospectively)

## Context

A cross-server transfer moves many entities over hours. It will be interrupted
-- by a network failure, an expired session, or a user. Restarting from the
beginning is not acceptable at that scale, so progress has to survive the
process.

## Decision

`core/state.py` keeps a `TransferStateStore` in SQLite: one row per entity with
its status, so a rerun resumes rather than repeats. SQLite rather than JSON
because the orchestrator updates state from multiple workers, and a
half-written JSON file after a crash loses the whole record.

## Consequences

- The state file is a real artifact with a schema. Changing that schema needs a
  migration path for anyone mid-transfer.
- Resume is only as accurate as the last committed status. An entity
  interrupted mid-upload may be retried, so entity transfers must tolerate
  being run twice.
- SQLite's single-writer model is a bottleneck if worker counts grow far beyond
  current levels. It is not one today.

## Alternatives considered

- **A JSON state file.** Rejected: concurrent writers, and a crash mid-write
  destroys the record it exists to protect.
- **Server-side state.** Rejected: needs a schema on the XNAT instance, which
  this tool deliberately does not require.
