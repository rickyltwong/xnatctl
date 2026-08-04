# 0002. Talk to XNAT over plain HTTP, not pyxnat

- **Status:** Accepted
- **Date:** 2026-08-04 (recorded retrospectively)

## Context

pyxnat is the established Python client for XNAT. Using it would have supplied
a resource model and session handling for free.

## Decision

xnatctl calls the XNAT REST API directly with `httpx`, and models responses
with its own Pydantic types. pyxnat is not a dependency.

## Consequences

- Every endpoint quirk is ours to discover and encode. Several are recorded in
  their own ADRs precisely because there is no library absorbing them --
  [ADR-0003](0003-retry-policy.md) and
  [ADR-0010](0010-scan-url-routing.md) are both of that kind.
- Streaming, connection pooling, per-phase timeouts and cancellation are
  directly controllable, which the upload and download paths depend on.
- Anything XNAT adds server-side is available immediately through
  `xnatctl api`, without waiting for library support.
- The cost is real: a maintainer debugging an XNAT behaviour cannot consult
  pyxnat's accumulated knowledge of it.

## Alternatives considered

- **Wrap pyxnat.** Rejected: its session and caching model is not compatible
  with the per-phase timeout and cancellation control the transfer paths need,
  and its resource abstractions would have to be unwrapped for the raw
  responses this tool prints.
