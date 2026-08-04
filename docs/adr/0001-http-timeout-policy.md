# 0001. Split the HTTP timeout into phases rather than one scalar

- **Status:** Accepted
- **Date:** 2026-08-04 (recorded retrospectively; decided during M1)

## Context

A single scalar timeout has to serve two incompatible cases. Downloading a
multi-gigabyte session legitimately takes hours, so the ceiling must be
generous. But an unreachable host must fail in seconds -- and under one scalar
it does not: the CLI hangs for the full window on a typo'd hostname or a
firewall drop.

The original configuration was a single 6-hour value, so both cases got 6
hours.

## Decision

Timeouts are per-phase, in `xnatctl/core/timeouts.py`:

| Phase | Value | Why |
|---|---|---|
| connect | 10s | An unreachable host is unreachable now, not in six hours. |
| read | 21600s (6h) | A large DICOM session transfer is legitimately this long. |
| pool | 30s | Waiting for a free connection is a local condition. |

Uploads override the read ceiling with a shorter per-request timeout, because a
single archive POST is not a six-hour operation even when the whole upload is.

## Consequences

- A genuinely slow *server response* to one request can still take six hours
  before failing. That is deliberate, and it is why the upload path narrows it.
- Anyone raising the connect timeout to "fix" a flaky network is treating the
  symptom: the retry policy ([ADR-0003](0003-retry-policy.md)) is what handles
  transient failures.
- The 6-hour read ceiling makes an interrupted operation feel unresponsive
  unless cancellation is cooperative -- see
  [ADR-0009](0009-cooperative-cancellation.md).

## Alternatives considered

- **One scalar, tuned down.** Rejected: any value short enough to catch an
  unreachable host will kill a legitimate large transfer.
- **No read timeout at all.** Rejected: a half-open connection then hangs the
  process forever, with no signal that anything is wrong.
