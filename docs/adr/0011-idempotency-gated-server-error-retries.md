# 0011. Retry 500/502/504 only where a repeat is safe

- **Status:** Accepted
- **Date:** 2026-08-07
- **Amends:** [ADR-0003](0003-retry-policy.md)

## Context

[ADR-0003](0003-retry-policy.md) set the core client to retry
`{429, 500, 502, 503, 504}`, and that set applied to every HTTP method. The
recorded reasoning was that a status code means the server answered, so unlike
a transport failure there is no ambiguity about whether the request ran.

That reasoning is sound for 429 and 503 and wrong for the rest. Both of those
are refusals the server issues *before* running any work -- which is also why
they are the two codes that carry `Retry-After`. A 500 is the opposite: the
handler ran and crashed, so side effects may already exist. A 504 is a gateway
saying the origin did not answer *in time*, not that it did not act.

The asymmetry inside our own client was the giveaway. The same wire events were
already treated as unsafe to repeat on a non-idempotent method:

| What happened | Without a proxy | Behind nginx |
|---|---|---|
| Connection dropped mid-response | `httpx.RemoteProtocolError` — not retried on POST | `502` — retried |
| Origin too slow to answer | read timeout — not retried on POST | `504` — retried |

So whether a POST was repeated depended on whether a reverse proxy sat in the
path. XNAT behind nginx is the ordinary deployment, so the riskier branch was
the common one. Nobody chose that; it fell out of classifying by status code in
one place and by transport exception in another.

The POSTs this reaches are not harmless: session import
(`services/import`), pipeline launch, prearchive archive, project create. A
504-triggered retry of an import races the still-running first import and
manufactures exactly the "concurrent modification" 400 that
`services/uploads.py` exists to survive.

## Decision

Split the retryable statuses by what they tell us about execution.

**Method-agnostic — `{429, 503}`.** Refusals issued before the work runs, so a
repeat cannot duplicate anything. Retried on any method, honouring
`Retry-After`.

**Idempotency-gated — `{500, 502, 504}`.** Retried only when the method is in
`IDEMPOTENT_METHODS`, or when the caller passes `retry_non_idempotent=True`.
This is the predicate the send-phase transport failures already use, so there
is one rule rather than two.

A gated code on a non-idempotent method raises `ServerError` immediately, on
the first response. Not `RetryExhaustedError` -- no retry was attempted, and
claiming exhaustion would be a lie. The exception carries a hint saying the
request may have partially executed and that server state is worth checking,
because "HTTP 504" alone reads as "nothing happened".

The two sets are derived from each other (`_AMBIGUOUS_RETRY_CODES =
RETRYABLE_STATUS_CODES - _METHOD_AGNOSTIC_RETRY_CODES`) so a status added later
cannot fall through both checks. Anything new is ambiguous until someone
deliberately declares it a pre-execution refusal.

## Consequences

A POST that hits a transient 500 or a slow gateway now fails on the first
response where it used to succeed on the second. That is the intended trade:
a visible failure the operator can retry deliberately, instead of a silent
duplicate archive or a second pipeline launch. Uploads are unaffected -- the
parallel and gradual paths use raw httpx clients with their own ladder
(ADR-0003) and never enter `_request`.

A caller that knows its POST is safe to repeat opts in per call with
`retry_non_idempotent=True`, which is how the catalog-refresh trigger already
works.

## Alternatives considered

**Leave it, and accept duplicate side effects as rare.** Rejected: the failure
is silent and the tool moves medical imaging data. A duplicated session import
is discovered later, by a human, as a mystery.

**Retry POSTs and detect duplicates afterwards.** Rejected as speculative: it
needs an idempotency key XNAT's import service does not accept, and it would
add a reconciliation path with no test coverage to justify it.

**Drop 500/502/504 from the retry set entirely.** Rejected: they are genuinely
worth retrying for the reads that make up most traffic, and a listing that
fails on one bad gateway response would be a regression for everyone.
