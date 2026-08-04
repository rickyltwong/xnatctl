# 0005. Give each upload worker its own httpx client

- **Status:** Accepted
- **Date:** 2026-08-04 (recorded retrospectively)

## Context

Parallel uploads run in a `ThreadPoolExecutor`. Sharing one `httpx.Client`
across those threads shares its connection pool and its cookie jar. The cookie
jar is the problem: the JSESSIONID lives there, and a re-authentication in one
worker mutates state every other worker is reading.

## Decision

`services/uploads.py` keeps its clients in a `threading.local()`, so each
worker builds and reuses its own. Token refresh is coordinated separately
through `_SessionRefresher`, which holds a lock so only the first thread to see
a 401 re-authenticates ([ADR-0004](0004-reauthentication.md)).

## Consequences

- Connections are not shared across workers, so the process holds up to
  `workers` pools rather than one. At the worker counts this tool uses (4-8)
  that is a fair trade.
- Anything added to the upload path must fetch its client through the
  thread-local accessor. A module-level client would reintroduce the shared
  cookie jar silently -- nothing would fail loudly, tokens would just
  occasionally go missing under load.

## Alternatives considered

- **One shared client with a lock around requests.** Rejected: serialises the
  parallel upload it exists to enable.
