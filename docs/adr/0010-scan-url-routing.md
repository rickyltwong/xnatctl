# 0010. Address scans through the subject, or through the flat experiment URL

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

XNAT does not route sub-resource suffixes under
`/data/projects/{P}/experiments/{E}`. It answers `/scans`, `/scans/{id}`,
`/scans/ALL/files` and `/files` with **200 and the parent experiment document**
-- a nonsense suffix returns the identical payload. Only `/resources` routes
there.

Verified live on one session:

```
200  items[]    rows=1   /data/projects/{P}/experiments/{E}/scans
200  items[]    rows=1   /data/projects/{P}/experiments/{E}/bogus123
200  ResultSet  rows=23  /data/projects/{P}/subjects/{S}/experiments/{E}/scans
200  ResultSet  rows=23  /data/experiments/{E}/scans
```

Everything failed silently. A listing found no `ResultSet` and returned zero
rows, so `scan list -P` reported no scans for sessions that had them.
`scan show -P` printed the session's fields under a scan heading. And
`scan delete -P` aimed a DELETE at a URL that resolves to the experiment.

## Decision

`HierarchyService.routable_scan_parent` normalises any experiment reference
before a scan path is built:

- With a subject, keep the project-scoped URL -- it routes.
- Without one, drop the project and use `/data/experiments/{E}`. Permissions
  are enforced server-side, so the project buys nothing in the URL.
- A genuine label cannot drop its project, so it is left alone and the CLI
  refuses rather than issuing a request that would act on the experiment.

`experiment_is_label` means "may be a label" -- callers set it whenever a
project is in scope, including for accession IDs -- so the accession-ID *shape*
decides, not the flag.

## Consequences

- Every scan path goes through the builder. Interpolating one by hand
  reintroduces the bug, and it fails silently, which is what made it survive.
- Scan URLs may not name the project. This is a change in what appears in logs
  and in `--verbose` output, not in what a user is permitted to do.
- `/resources` on the project-scoped experiment prefix is left alone, because
  it genuinely routes. That asymmetry is XNAT's, not ours.

## Alternatives considered

- **Always use the flat form.** Rejected: with a subject available the
  project-scoped URL routes and is more precise about intent.
- **Resolve the subject on every call.** Rejected: an extra round trip per
  scan operation to recover information the flat URL does not need.
