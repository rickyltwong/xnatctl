# 0004. Re-authenticate transparently, and propagate the new token

- **Status:** Accepted
- **Date:** 2026-08-04
- **Supersedes:** the earlier "`auto_reauth` defaults to False" behaviour

## Context

XNAT retires a JSESSIONID after roughly 15 minutes of inactivity. Operations
here routinely outlast that: a large upload, a prearchive archive, a
cross-server transfer.

Three mechanisms existed and did not compose. `XNATClient.auto_reauth`
defaulted to False and the CLI never set it, so the client's 401 branch was
dead in every invocation. `@require_auth` only checked at command start.
Worker threads refreshed into their own copy of the token and wrote it back
nowhere, so the phases after a multi-hour upload went out with the token the
command began with.

## Decision

- `XNATClient.auto_reauth` still defaults to False for library users, who may
  want a 401 surfaced rather than papered over. The CLI sets it to True
  whenever a password is available.
- A worker-thread refresh publishes the new token to the owning client, and
  best-effort to the on-disk cache so a concurrent process benefits. Both
  writes are best-effort: a worker holding a fresh token must not fail an
  upload because a cache could not be written.
- 401 means re-authenticate once and retry; it is never counted as a retryable
  status ([ADR-0003](0003-retry-policy.md)).

## Consequences

- A command with no password (token-only, via `XNAT_TOKEN`) cannot self-heal.
  It still raises `SessionExpiredError`, which is the honest outcome.
- Transparent reauth can mask a server that is expiring sessions
  pathologically fast. The reauth is logged at INFO for that reason.

## Alternatives considered

- **Default `auto_reauth` to True everywhere.** Rejected: a library caller
  embedding xnatctl may need to see the 401 rather than have credentials
  replayed automatically.
