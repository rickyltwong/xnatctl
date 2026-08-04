# Architecture Decision Records

Decisions that constrain future code, with the reasoning that produced them.

An ADR exists so the next person -- or the next agent -- does not "clean up"
something load-bearing. Several entries here record behaviour that looks like a
mistake until you know what it is working around: a 6-hour timeout, a lint rule
left off, a URL that omits the project.

Write one when a decision would be surprising to someone reading only the code.
Use [template.md](template.md). Number sequentially; never renumber. Superseded
records stay, marked as superseded and pointing at what replaced them.

| # | Decision | Status |
|---|---|---|
| [0001](0001-http-timeout-policy.md) | Split the HTTP timeout into phases rather than one scalar | Accepted |
| [0002](0002-pure-http-no-pyxnat.md) | Talk to XNAT over plain HTTP, not pyxnat | Accepted |
| [0003](0003-retry-policy.md) | Retry only what retrying can fix | Accepted |
| [0004](0004-reauthentication.md) | Re-authenticate transparently, and propagate the new token | Accepted |
| [0005](0005-thread-local-http-clients.md) | Give each upload worker its own httpx client | Accepted |
| [0006](0006-session-token-cache.md) | Cache the session token in an owner-private file, expiring on idleness | Accepted |
| [0007](0007-transfer-state-persistence.md) | Persist cross-server transfer state in SQLite | Accepted |
| [0008](0008-lazy-imports.md) | Import inside functions where it breaks a cycle or defers a cost | Accepted |
| [0009](0009-cooperative-cancellation.md) | Cancel parallel work cooperatively, and make the wait interruptible | Accepted |
| [0010](0010-scan-url-routing.md) | Address scans through the subject, or through the flat experiment URL | Accepted |

## Related

`docs/plans/` holds dated design and implementation plans. Those record *how*
something was built; these record *why* a constraint exists. A plan is finished
when the work ships; an ADR stays true until superseded.
