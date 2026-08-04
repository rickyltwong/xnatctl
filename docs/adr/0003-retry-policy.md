# 0003. Retry only what retrying can fix

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

XNAT returns the same status code for conditions that differ completely in
whether waiting helps. Its import service answers 400 both for "two uploads met
in the same session, try again" and for "that project does not exist". Treating
them alike meant a mislabeled upload retried every file five times across 62
seconds of backoff, then repeated that in each of two salvage passes: measured
at 403 HTTP attempts for 30 files, still going when cut off at 30 minutes.

## Decision

Two policies, deliberately different.

**Core client** (`core/client.py`): retry `{429, 500, 502, 503, 504}` with full
jitter on exponential backoff, honouring `Retry-After` (delta-seconds or
HTTP-date, capped at 300s). 401 triggers re-authentication, not a retry --
see [ADR-0004](0004-reauthentication.md).

**Upload path** (`services/uploads.py`) additionally treats 400 as retryable,
because XNAT's import service returns transient 400s when concurrent uploads
meet in one session. It discriminates by response body:

- *Transient* means concurrency and nothing else -- "Session processing in
  progress", "Duplicate archive attempt", "Destination session in use".
- *Permanent* means configuration or data -- unknown project, unidentifiable
  subject, "retry with overwrite enabled", scan UID conflicts.

Unrecognised bodies are retried. A `RetryBudget` caps total backoff for an
operation (15 minutes by default).

The signatures were read out of the classes compiled into a deployed XNAT
**1.9.2.1** (`PrearcSessionArchiver`, `PrearcUtils`, `Importer`), not guessed,
and cross-checked against a real `ClientException` in that server's
`prearchive.log`.

## Consequences

- The signature lists are version-specific. When XNAT rewords a message, an
  unrecognised *permanent* 400 is retried again -- slowly, but bounded by the
  budget. That is the failure mode we chose.
- Retrying unknown 400s is deliberate. The inverse -- an allowlist of known
  transient messages -- would turn uploads that currently succeed into
  immediate failures the moment XNAT changes wording. Slow is recoverable;
  refusing a good upload is not.
- A non-idempotent POST is not retried after a read-phase timeout, because the
  server may have already acted on it.

## Alternatives considered

- **Allowlist transient signatures, fail everything else fast.** Rejected for
  the drift asymmetry above.
- **Never retry 400.** Rejected: the concurrency case is real and common on
  busy servers, and is why 400 was made retryable originally.
