# 0006. Cache the session token in an owner-private file, expiring on idleness

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Re-authenticating on every invocation is slow and, on a server using a shared
service account, actively harmful: each login spawns a server-side session, and
exhausting the concurrent-session limit produces intermittent 401s across every
client using that account.

Two defects shaped the current design. The cache was written with the process
umask and chmod'ed afterwards, leaving a window where the token was
world-readable -- and the chmod failure was swallowed. And expiry was measured
from *creation*, so a session in constant use was discarded 15 minutes after
login even though the server considers it live.

## Decision

`~/.config/xnatctl/.session` holds the token as JSON, written through a
private temp file and `os.replace`, so it is 0600 from the first byte and
readers never observe a partial write.

Expiry slides: the window is measured from `last_used_at`, which `load_session`
touches on every read. `created_at` is kept for diagnostics.

## Consequences

- The mode is a POSIX guarantee only. Windows reports 0666 for any writable
  file regardless of its ACL, and `chmod` there only toggles the read-only
  flag, so **the owner-private property does not hold on Windows.** Protecting
  it there needs ACL work that this tool does not do.
- Sliding expiry means the cache can hold a token the server has already
  retired -- for instance after a server restart. That is safe because the
  server is the authority: the stale token 401s and reauth
  ([ADR-0004](0004-reauthentication.md)) handles it. Sliding can only avoid
  needless logins, never extend a session past its real life.
- Every read now writes. A read-only home directory degrades to the previous
  behaviour rather than failing the command.

## Alternatives considered

- **No cache.** Rejected: on shared service accounts it causes the very
  session exhaustion described above.
- **OS keychain for the session token.** Rejected for the *session* token,
  which is short-lived and per-host. The keychain is used for the long-lived
  *password* instead (`xnatctl config set-password`).
