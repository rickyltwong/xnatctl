# 0009. Cancel parallel work cooperatively, and make the wait interruptible

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Ctrl+C during a parallel upload could hang for the better part of a minute and
then exit with a code indistinguishable from a crash.

The cause was not the work queue, which is what the symptom suggested. It was
the retry ladder: `upload_with_retry` climbs 2+4+8+16+32s on `time.sleep`,
which nothing can shorten, so every in-flight batch had to finish sleeping
before the process could unwind. Measured against an always-503 server with two
workers interrupted mid-retry: **50.8s before, 0.1s after.**

Separately, Click converts `KeyboardInterrupt` into `Abort` and exits 1 before
any xnatctl code runs, so a user-cancelled run looked like a general error
despite the exit-code taxonomy defining `USER_CANCELLED`.

## Decision

`core/cancellation.py` provides:

- `CancellationToken` -- a flag the main thread sets and workers poll, with an
  interruptible `sleep()` that replaces `time.sleep` in every retry backoff.
- `cancellable_pool` -- replaces `with ThreadPoolExecutor(...)`, calling
  `shutdown(wait=False, cancel_futures=True)` before the pool is joined, so the
  backlog is dropped rather than drained.

It lives in `core/` so a later refactor of the command layer cannot strand it.
`handle_errors` catches `KeyboardInterrupt` inside the command body, before
Click sees it, and exits `USER_CANCELLED`.

A cancelled unit of work is reported as *cancelled*, not failed. Telling
someone the server rejected data they themselves stopped sending is a lie.

## Consequences

- Cancellation is cooperative. A worker inside an HTTP request finishes that
  request; the bound is the read timeout
  ([ADR-0001](0001-http-timeout-policy.md)), not instant.
- The pool is always joined on the way out. Callers delete temp archives in
  their own `finally` blocks, and returning while a worker still holds one open
  would trade a slow exit for a corrupt one.
- Any new retry loop that calls `time.sleep` silently opts out. Tests that
  neutralise backoff must patch `CancellationToken.sleep` -- patching
  `time.sleep` no longer has any effect, and a suite that does so will simply
  run slowly rather than fail.

## Alternatives considered

- **Rely on `cancel_futures` alone.** Insufficient, and in the upload path
  nearly irrelevant: `upload_dicom_parallel` derives its batch count from the
  worker count, so there is no backlog to drop. It does matter for the CLI
  loops, where items outnumber workers.
- **Kill worker threads.** Not possible in Python, and would leave partial
  archives behind if it were.
