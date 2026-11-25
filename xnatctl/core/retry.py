"""Retry policy for XNAT operations: the single home for what gets retried.

Three consumers share this module:

* ``core/client.py`` -- the ``XNATClient._request`` ladder consumes the
  status-code sets, idempotency rule, transport-error taxonomy, and the
  backoff/Retry-After helpers. The ladder itself stays in the client because
  it is inseparable from auth refresh and response handling.
* ``services/upload/`` -- the raw-httpx import paths call
  :func:`upload_with_retry`, the response-based ladder that adds the
  transient-vs-permanent HTTP 400 discrimination XNAT's import service needs.
* ``services/transfer/executor.py`` -- wraps its import POST in
  :func:`retry_call`, the generic call-again primitive.

Policy summary (per operation class):

* Client requests: ``429``/``503`` retry on any method (pre-execution
  refusals); ``500``/``502``/``504`` only for idempotent methods.
* Import uploads: additionally retry ``400``, because XNAT's import service
  returns transient 400s while archive operations race -- except the 400s
  whose body matches a known-permanent message (see
  :data:`PERMANENT_400_SIGNATURES`).
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken
from xnatctl.core.exceptions import OperationCancelledError
from xnatctl.core.logging import FILE_ONLY_ATTR

logger = logging.getLogger(__name__)

T = TypeVar("T")


RETRY_BACKOFF_BASE = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_METHOD_AGNOSTIC_RETRY_CODES = {429, 503}
_RETRY_AFTER_STATUS_CODES = {429, 503}
_AMBIGUOUS_RETRY_CODES = RETRYABLE_STATUS_CODES - _METHOD_AGNOSTIC_RETRY_CODES
_MAX_RETRY_AFTER_SECONDS = 300

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})

RETRYABLE_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.CloseError,
)
PERMANENT_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.UnsupportedProtocol,
    httpx.DecodingError,
    httpx.InvalidURL,
    httpx.TooManyRedirects,
    httpx.LocalProtocolError,
)


UPLOAD_MAX_RETRIES = 5
UPLOAD_RETRY_BACKOFF_BASE = 2

UPLOAD_ONLY_RETRYABLE_STATUS_CODES = {
    400,
}
UPLOAD_RETRYABLE_STATUS_CODES = RETRYABLE_STATUS_CODES | UPLOAD_ONLY_RETRYABLE_STATUS_CODES

# Harvested 2026-08-04 from the messages compiled into a deployed XNAT 1.9.2.1
# (org.nrg.xnat.archive.PrearcSessionArchiver, PrearcUtils,
# restlet.services.Importer) and cross-checked against a real ClientException
# in that server's prearchive.log: read off the running server, not guessed.
# Re-check them there when a newer XNAT changes its wording. The transient ones
# are all concurrency (two uploads meeting in one session); the permanent ones
# are configuration or data errors that no wait fixes.
TRANSIENT_400_SIGNATURES = (
    "session processing in progress",
    "concurrent modification is discouraged",
    "duplicate archive attempt",
    "destination session in use",
)

PERMANENT_400_SIGNATURES = (
    "unable to deduce session label",
    "unable to identify subject",
    "unable to identify destination project",
    "not allowed to create new subjects",
    "unable to create new subject id",
    "unable to create new session id",
    "session already exists, retry with overwrite enabled",
    "via archive process",
    "session already contains a scan",
    "already exists for another subject",
    "src uri is invalid",
    "expected a catalog file, however it was missing",
    "non-standard prearchive structure",
    "or non-parsable dicom) files",
    "is required when requesting",
)


def is_permanent_400(body: str) -> bool:
    """Whether a 400 body names a fault that retrying cannot fix.

    Deliberately a denylist of *known-permanent* messages rather than an
    allowlist of known-transient ones. Both fix the pathology this addresses --
    a misconfigured upload always produces one of the messages above, and now
    fails on the first attempt instead of the sixth. The difference is how each
    behaves when XNAT's wording drifts:

    * denylist: an unrecognised 400 is retried, as it is today. A new transient
      message costs some backoff.
    * allowlist: an unrecognised 400 fails immediately. A new transient message
      turns uploads that currently succeed into failures.

    Slow is recoverable; spuriously refusing a good upload is not. The retry
    budget bounds the slow case, so drift degrades rather than breaks.
    """
    if not body:
        return False
    haystack = body.lower()
    if any(sig in haystack for sig in TRANSIENT_400_SIGNATURES):
        return False
    return any(sig in haystack for sig in PERMANENT_400_SIGNATURES)


def is_retryable_status(status_code: int) -> bool:
    """Check if an HTTP status code warrants an upload retry.

    Retryable: 400 (XNAT transient), 429 (rate limit), 5xx (server errors).
    Non-retryable: 2xx (success), 401/403 (auth), other 4xx (client error).
    """
    return status_code in UPLOAD_RETRYABLE_STATUS_CODES


class RetryBudget:
    """A ceiling on total backoff across one upload operation.

    The signature lists above are read off one XNAT version; a future release
    can word things differently, and an unrecognised permanent 400 would then
    be retried on every file again. This bounds that worst case regardless of
    what the server says: once the operation has spent its budget sleeping,
    further retries are abandoned and the failures reported.
    """

    __slots__ = ("_lock", "_remaining", "_total")

    def __init__(self, seconds: float = 900.0) -> None:
        self._total = seconds
        self._remaining = seconds
        self._lock = threading.Lock()

    def claim(self, seconds: float) -> bool:
        """Reserve a backoff sleep. False when the budget is exhausted."""
        with self._lock:
            if self._remaining < seconds:
                return False
            self._remaining -= seconds
            return True

    @property
    def exhausted(self) -> bool:
        """Whether any budget remains."""
        with self._lock:
            return self._remaining <= 0


_RNG = random.Random()


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    if resp.status_code not in _RETRY_AFTER_STATUS_CODES:
        return None
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None

    raw = raw.strip()
    seconds: float | None = None
    try:
        seconds = float(int(raw))
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()

    if seconds is None:
        return None
    seconds = max(seconds, 0.0)
    if seconds <= _MAX_RETRY_AFTER_SECONDS:
        return seconds
    return None


def _backoff_delay(attempt: int, rng: random.Random = _RNG) -> float:
    return rng.uniform(0.0, float(RETRY_BACKOFF_BASE ** (attempt + 1)))


def retry_call(
    fn: Callable[[], T],
    *,
    retryable: Callable[[Exception], bool],
    max_attempts: int = 3,
    backoff_base: float = float(RETRY_BACKOFF_BASE),
    label: str = "operation",
) -> T:
    """Call ``fn`` again on retryable failures, with exponential backoff.

    The generic primitive for call sites that are not shaped like the client
    ladder (typed-error requests) or the upload ladder (raw responses): the
    caller supplies a ``retryable`` predicate and this owns the loop. An
    exception the predicate rejects propagates immediately -- a programming
    error must never be retried into a 3x-slower programming error.

    Args:
        fn: Zero-argument callable to execute. Called once per attempt, so any
            per-attempt setup (reopening a file, rebuilding a request) belongs
            inside it.
        retryable: Predicate deciding whether a raised exception is worth
            another attempt.
        max_attempts: Total attempts including the first (must be >= 1).
        backoff_base: Seconds before the second attempt; doubles each retry
            (``backoff_base * 2**attempt``). Must be finite and >= 0.
        label: Name used in retry log lines.

    Returns:
        Whatever ``fn`` returns.

    Raises:
        ValueError: If ``max_attempts`` < 1, or ``backoff_base`` is negative
            or not finite.
        Exception: The last retryable exception once attempts are exhausted,
            or the first non-retryable one immediately.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if not math.isfinite(backoff_base) or backoff_base < 0:
        raise ValueError(f"backoff_base must be finite and >= 0, got {backoff_base}")

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if not retryable(e):
                raise
            last_exc = e
            if attempt < max_attempts - 1:
                delay = backoff_base * (2**attempt)
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                    label,
                    attempt + 1,
                    max_attempts,
                    delay,
                    e,
                )
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


_RETRY_DEBUG_MAX_SNIPPETS = 20
_retry_debug_snippets_emitted = 0
_retry_debug_lock = threading.Lock()


def _safe_body(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:
        logger.exception("could not read the upload response body", extra={FILE_ONLY_ATTR: True})
        return ""


def _debug_log_retryable_400_body(resp: httpx.Response, label: str) -> None:
    global _retry_debug_snippets_emitted
    with _retry_debug_lock:
        if _retry_debug_snippets_emitted >= _RETRY_DEBUG_MAX_SNIPPETS:
            return
        _retry_debug_snippets_emitted += 1
    snippet = _safe_body(resp).strip().replace("\n", " ")
    if not snippet:
        return
    try:
        logger.debug("%s: retryable HTTP 400 body: %s", label, snippet[:200])
    except Exception:
        # Optional output: a handler that fails on this line must not end the retries.
        logger.exception("could not log a retryable HTTP 400 body", extra={FILE_ONLY_ATTR: True})


def _stop_instead_of_backoff(
    label: str,
    delay: int,
    retry_budget: RetryBudget | None,
    cancel_token: CancellationToken,
) -> bool:
    if retry_budget is not None and not retry_budget.claim(delay):
        logger.warning("%s: retry budget exhausted, giving up", label)
        return True
    return cancel_token.sleep(delay)


def upload_with_retry(
    upload_fn: Callable[[], httpx.Response],
    *,
    max_retries: int = UPLOAD_MAX_RETRIES,
    backoff_base: int = UPLOAD_RETRY_BACKOFF_BASE,
    label: str = "upload",
    cancel_token: CancellationToken = NULL_TOKEN,
    retry_budget: RetryBudget | None = None,
) -> httpx.Response:
    """Execute an upload function with retry on transient HTTP errors.

    Args:
        upload_fn: Callable that performs the upload and returns an httpx.Response.
                   Will be called multiple times on retry -- must be idempotent.
        max_retries: Maximum number of retries (default: 5).
        backoff_base: Base for exponential backoff in seconds (default: 2).
        label: Label for log messages.
        cancel_token: Checked before each attempt, and slept against instead of
            ``time.sleep``. The ladder is 2+4+8+16+32s, so without this a
            cancelled upload sits in a backoff for up to a minute per in-flight
            batch after the user has already asked it to stop.
        retry_budget: Shared ceiling on total backoff for the whole operation.
            When it runs out, the last response is returned instead of sleeping
            again. Bounds the damage if a permanent 400 goes unrecognised.

    Returns:
        The httpx.Response from a successful attempt.

    Raises:
        The last exception if all retries are exhausted and no response was obtained.
    """
    last_resp = None
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        if cancel_token.cancelled:
            break
        try:
            resp = upload_fn()
            if not is_retryable_status(resp.status_code):
                return resp
            if resp.status_code == 400 and is_permanent_400(_safe_body(resp)):
                logger.debug("%s: permanent HTTP 400, not retrying", label)
                return resp
            last_resp = resp
            last_exc = None
            if attempt < max_retries:
                delay = backoff_base ** (attempt + 1)
                if resp.status_code == 400 and logger.isEnabledFor(logging.DEBUG):
                    _debug_log_retryable_400_body(resp, label)
                logger.warning(
                    "%s: HTTP %d on attempt %d/%d, retrying in %ds",
                    label,
                    resp.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                if _stop_instead_of_backoff(label, delay, retry_budget, cancel_token):
                    break
        except httpx.ConnectTimeout:
            logger.warning("%s: connect timed out; not retrying", label)
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            last_resp = None
            if attempt < max_retries:
                delay = backoff_base ** (attempt + 1)
                detail = f"{type(e).__name__}: {str(e).strip().replace(chr(10), ' ')}"
                logger.warning(
                    "%s: %s on attempt %d/%d, retrying in %ds",
                    label,
                    detail,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                if _stop_instead_of_backoff(label, delay, retry_budget, cancel_token):
                    break

    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    if cancel_token.cancelled:
        raise OperationCancelledError(label)
    raise RuntimeError(f"{label}: all retries exhausted with no response")
