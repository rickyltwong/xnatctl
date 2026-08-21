"""Retry-ladder HTTP transport backing :meth:`XNATClient.stream`/``_request``.

Split out of :mod:`xnatctl.core.client`: the shared machinery for turning an
httpx call into XNAT's typed-exception contract -- status-code classification,
transport-failure translation, and the retry loop itself. These are plain
functions that take the client explicitly rather than a mixin, mirroring the
codebase's existing "helper takes the client object" pattern (see
:func:`xnatctl.services.downloads.stream_to_file`). ``XNATClient.stream`` and
``XNATClient._request`` are thin public wrappers around :func:`stream` and
:func:`request` below; see those methods for the documented public contract.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

from xnatctl.core.exceptions import (
    AuthenticationError,
    ClientRequestError,
    NetworkError,
    PermissionDeniedError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
    SessionExpiredError,
)
from xnatctl.core.exceptions import (
    RequestTimeoutError as XNATTimeoutError,
)
from xnatctl.core.redact import redact_url_query
from xnatctl.core.retry import (
    _AMBIGUOUS_RETRY_CODES,
    IDEMPOTENT_METHODS,
    PERMANENT_TRANSPORT_ERRORS,
    RETRYABLE_STATUS_CODES,
    RETRYABLE_TRANSPORT_ERRORS,
    _backoff_delay,
    _retry_after_seconds,
)
from xnatctl.core.timeouts import DEFAULT_CONNECT_TIMEOUT_SECONDS, build_httpx_timeout

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

logger = logging.getLogger(__name__)

_BODY_SNIPPET_CHARS = 200


def _body_snippet(resp: httpx.Response) -> str:
    """Return a short, redacted snippet of a response body for error details."""
    try:
        return redact_url_query(resp.text[:_BODY_SNIPPET_CHARS])
    except Exception:
        return ""


def _read_stream_body(resp: httpx.Response) -> None:
    """Buffer a streaming response's body so ``.text`` is available for errors.

    httpx will not expose ``.text`` on a streamed response until it is read.
    Guarded because a truncated error body must not eclipse the status it
    describes.
    """
    try:
        resp.read()
    except Exception:
        pass


# Transport failures worth translating when opening a stream: the same set
# ``request`` handles, so no raw httpx exception escapes either path.
_STREAM_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.TimeoutException,
    *PERMANENT_TRANSPORT_ERRORS,
    *RETRYABLE_TRANSPORT_ERRORS,
)


def _raise_for_xnat_status(base_url: str, resp: httpx.Response, method: str, path: str) -> None:
    """Raise the typed exception a terminal error status maps to.

    The single home for XNAT's status->exception mapping, shared by the
    ``request`` ladder and ``stream``. Returns without raising for 2xx and
    for retryable statuses, whose retry/reauth control flow stays with each
    caller -- this helper only owns the raising. A 401 maps straight to
    :class:`SessionExpiredError`; the decision to reauth first belongs to
    the caller. For 4xx/5xx bodies used in the message, the response must
    already be readable (a streaming response needs ``read()`` first).
    """
    code = resp.status_code
    if code == 401:
        expired_err = SessionExpiredError(base_url)
        expired_err.details.update({"status_code": code, "method": method, "path": path})
        raise expired_err
    if code == 403:
        denied_err = PermissionDeniedError(
            resource=path,
            operation=method.lower(),
            url=base_url,
        )
        denied_err.details.update({"status_code": code, "method": method, "path": path})
        raise denied_err
    if code == 404:
        missing_err = ResourceNotFoundError("resource", path)
        missing_err.details.update({"status_code": code, "method": method, "path": path})
        raise missing_err
    if code in RETRYABLE_STATUS_CODES:
        return
    if code >= 400:
        if code < 500:
            raise ClientRequestError(code, method, path, _body_snippet(resp))
        raise ServerError(code, method, path, _body_snippet(resp))


def _stream_transport_error(
    base_url: str,
    exc: Exception,
    *,
    method_upper: str,
    path: str,
    read_timeout: int,
    may_retry_after_send: bool,
) -> Exception:
    """Translate a stream-open transport failure, mirroring ``request``.

    Returns a retryable ``last_error`` for failures that may be retried, or
    raises the terminal typed error for the fail-fast cases (connect
    timeout, permanent transport errors, and send-phase failures on a
    non-idempotent method).
    """
    if isinstance(exc, httpx.ConnectTimeout):
        raise XNATTimeoutError(base_url, DEFAULT_CONNECT_TIMEOUT_SECONDS) from exc
    if isinstance(exc, httpx.ConnectError):
        return ServerUnreachableError(base_url)
    if isinstance(exc, PERMANENT_TRANSPORT_ERRORS):
        raise NetworkError(base_url, f"{type(exc).__name__}: {exc}") from exc
    if isinstance(exc, httpx.TimeoutException):
        if not may_retry_after_send:
            raise XNATTimeoutError(
                base_url,
                read_timeout,
                f"{method_upper} {path} timed out after the request was sent; "
                "it may have partially executed on the server. Not retried "
                "automatically - check server state before repeating it.",
            ) from exc
        return NetworkError(base_url, f"Timeout after {read_timeout}s")
    # RETRYABLE_TRANSPORT_ERRORS: send-phase hazard, same idempotency rule.
    if not may_retry_after_send:
        raise NetworkError(
            base_url,
            f"{type(exc).__name__} on {method_upper} {path} after the request "
            "was sent; it may have partially executed on the server. "
            "Not retried automatically.",
        ) from exc
    return NetworkError(base_url, f"{type(exc).__name__}: {exc}")


@contextmanager
def stream(
    client: XNATClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
) -> Iterator[httpx.Response]:
    """Implementation behind :meth:`XNATClient.stream`; see there for the public contract.

    Args:
        client: The XNATClient this call streams through.
        method: HTTP method (GET in practice; the idempotency check stays
            honest for anything else).
        path: API path.
        params: Query parameters.
        headers: Additional request headers.
        timeout: Read-timeout override in seconds.

    Yields:
        The open streaming response (2xx). Closed on context exit.

    Raises:
        SessionExpiredError, PermissionDeniedError, ResourceNotFoundError,
        ClientRequestError, ServerError: mapped from the error status.
        RequestTimeoutError, NetworkError, ServerUnreachableError: from transport
            failures, mirroring ``request``.
        RetryExhaustedError: when retries drain.
    """
    http_client = client._get_client()
    read_timeout = timeout or client.timeout
    request_timeout = build_httpx_timeout(read_timeout)
    method_upper = method.upper()
    may_retry_after_send = method_upper in IDEMPOTENT_METHODS
    last_error: Exception | None = None
    did_reauth = False

    attempt = 0
    while attempt <= client.max_retries:
        # Send the session cookie explicitly per call rather than mutating
        # the shared ``client.cookies`` jar (the httpx-0.28 workaround
        # ``request`` uses): stream() runs on parallel worker threads over
        # one shared httpx.Client, and mutating the shared jar there races.
        # The token is read once so the reauth path below can tell whether
        # another thread already refreshed it while this call was in flight.
        token_at_send = client.session_token
        call_headers = dict(headers) if headers else {}
        if token_at_send:
            cookie = f"JSESSIONID={token_at_send}"
            existing = call_headers.get("Cookie")
            call_headers["Cookie"] = f"{existing}; {cookie}" if existing else cookie
        auth = client._get_auth()

        try:
            request = http_client.build_request(
                method,
                path,
                params=params,
                headers=call_headers,
                timeout=request_timeout,
            )
            response = http_client.send(request, auth=auth, stream=True)
        except _STREAM_TRANSPORT_ERRORS as e:
            last_error = _stream_transport_error(
                client.base_url,
                e,
                method_upper=method_upper,
                path=path,
                read_timeout=read_timeout,
                may_retry_after_send=may_retry_after_send,
            )
        else:
            code = response.status_code
            logger.debug(
                "%s %s -> %d (stream, attempt %d/%d)",
                method_upper,
                redact_url_query(str(response.request.url)),
                code,
                attempt + 1,
                client.max_retries + 1,
            )

            can_reauth = (
                client.auto_reauth and not did_reauth and bool(client.username and client.password)
            )
            if code == 401 and can_reauth:
                response.close()
                with client._reauth_lock:
                    # A parallel stream may have refreshed the session
                    # while this request was in flight; reuse its token
                    # instead of opening yet another server session.
                    if client.session_token == token_at_send:
                        client.authenticate()
                did_reauth = True
                continue

            if code in RETRYABLE_STATUS_CODES:
                _read_stream_body(response)
                last_error = ServerError(code, method, path, _body_snippet(response))

                if code in _AMBIGUOUS_RETRY_CODES and not may_retry_after_send:
                    response.close()
                    ambiguous = ServerError(code, method, path, _body_snippet(response))
                    ambiguous.hint = (
                        f"The server sent {code} after receiving the "
                        f"{method_upper}, so it may have partially executed. It was "
                        "not retried automatically -- check server state before "
                        "repeating it."
                    )
                    raise ambiguous

                if attempt < client.max_retries:
                    retry_after = _retry_after_seconds(response)
                    delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                    response.close()
                    # WARNING for the same reason ``request`` warns: a silent
                    # retry storm reads as "the download is hanging".
                    logger.warning(
                        "HTTP %d on %s %s; retrying in %.1fs%s (attempt %d/%d)",
                        code,
                        method_upper,
                        path,
                        delay,
                        " per Retry-After" if retry_after is not None else "",
                        attempt + 1,
                        client.max_retries + 1,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                response.close()
                raise RetryExhaustedError("request", client.max_retries + 1, last_error)

            if code >= 400:
                # Terminal error: read the body so the typed exception can
                # quote it, then map and raise through the shared helper.
                _read_stream_body(response)
                try:
                    _raise_for_xnat_status(client.base_url, response, method, path)
                finally:
                    response.close()

            try:
                yield response
            except httpx.TimeoutException as e:
                # Mid-body failures are translated but never retried: a
                # partially consumed stream cannot be resumed.
                raise XNATTimeoutError(
                    client.base_url,
                    read_timeout,
                    f"{method_upper} {path} timed out while streaming the body; "
                    "the partial download was discarded.",
                ) from e
            except httpx.HTTPError as e:
                raise NetworkError(
                    client.base_url,
                    f"{type(e).__name__} while streaming the {method_upper} {path} body",
                ) from e
            finally:
                response.close()
            return

        # Transport failure left a retryable ``last_error``; back off.
        if attempt < client.max_retries:
            delay = _backoff_delay(attempt)
            logger.warning(
                "%s on %s %s; retrying in %.1fs (attempt %d/%d)",
                type(last_error).__name__ if last_error else "Transport error",
                method_upper,
                path,
                delay,
                attempt + 1,
                client.max_retries + 1,
            )
            time.sleep(delay)
        attempt += 1

    raise RetryExhaustedError("request", client.max_retries + 1, last_error)


def request(  # noqa: C901  # pre-existing; see pyproject
    client: XNATClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    data: Any | None = None,
    content: Any | None = None,
    files: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
    retry_non_idempotent: bool = False,
) -> httpx.Response:
    """Implementation behind :meth:`XNATClient._request`; see there for the public contract.

    Args:
        client: The XNATClient this call runs through.
        method: HTTP method.
        path: API path.
        params: Query parameters.
        json: JSON body.
        data: Form data or raw body.
        content: Raw request body (bytes, str, iterator, or file object).
            Use this for streaming raw file uploads instead of ``data``.
        files: Files to upload.
        headers: Additional headers.
        timeout: Request timeout override.
        retry_non_idempotent: Allow read-phase retries for a method outside
            :data:`IDEMPOTENT_METHODS`. Opt in per call site, only where the
            operation is provably safe to repeat -- a retried POST can mean a
            double archive or a double pipeline launch.

    Returns:
        HTTP response (2xx only; error statuses raise typed exceptions).

    Raises:
        SessionExpiredError: On 401 when reauth is unavailable/exhausted.
        PermissionDeniedError: On 403.
        ResourceNotFoundError: On 404.
        ClientRequestError: On any other 4xx.
        ServerError: On a 5xx that is not retryable or after retries drain.
        RequestTimeoutError: On a connect-phase timeout (fails fast, not retried),
            or on a read-phase timeout for a non-idempotent method.
        RetryExhaustedError: When retryable statuses or connect/read-timeout
            failures exhaust ``max_retries``.
    """
    http_client = client._get_client()

    # Read/write ceiling for this call (int, also used in error messages).
    # Wrapped in a structured httpx.Timeout so a per-request override cannot
    # re-flatten connect back to the multi-hour scalar.
    read_timeout = timeout or client.timeout
    request_timeout = build_httpx_timeout(read_timeout)
    last_error: Exception | None = None
    did_reauth = False
    may_retry_after_send = method.upper() in IDEMPOTENT_METHODS or retry_non_idempotent

    attempt = 0
    while attempt <= client.max_retries:
        # Set the session cookie on the client instance rather than passing
        # ``cookies=`` per request (httpx 0.28 deprecates per-request
        # cookies). Refreshed each iteration so a mid-loop reauth is picked
        # up on the retry.
        if client.session_token:
            http_client.cookies.set("JSESSIONID", client.session_token)
        else:
            http_client.cookies.delete("JSESSIONID")
        auth = client._get_auth()
        started = time.monotonic()
        try:
            resp = http_client.request(
                method,
                path,
                params=params,
                json=json,
                data=data if data is not None else None,
                content=content,
                files=files,
                headers=headers,
                auth=auth,
                timeout=request_timeout,
            )

            # One line per attempt is the backbone of `-v` diagnostics: it
            # is what tells a user whether a slow command is stuck on one
            # request or grinding through retries. The full URL
            # goes through redaction -- query strings carry tokens.
            logger.debug(
                "%s %s -> %d in %dms (attempt %d/%d)",
                method.upper(),
                redact_url_query(str(resp.request.url)),
                resp.status_code,
                (time.monotonic() - started) * 1000,
                attempt + 1,
                client.max_retries + 1,
            )

            # Handle auth errors
            if resp.status_code == 401:
                if client.auto_reauth and not did_reauth and client.username and client.password:
                    logger.debug("401 on %s %s; re-authenticating and retrying once", method, path)
                    client.authenticate()
                    did_reauth = True
                    continue

                logger.debug(
                    "401 on %s %s; not re-authenticating (auto_reauth=%s, "
                    "already_retried=%s, credentials=%s)",
                    method,
                    path,
                    client.auto_reauth,
                    did_reauth,
                    bool(client.username and client.password),
                )

                _raise_for_xnat_status(client.base_url, resp, method, path)

            if resp.status_code in (403, 404):
                _raise_for_xnat_status(client.base_url, resp, method, path)

            # Retry on retryable statuses; on exhaustion raise a typed
            # RetryExhaustedError rather than leaking httpx.HTTPStatusError.
            if resp.status_code in RETRYABLE_STATUS_CODES:
                last_error = ServerError(resp.status_code, method, path, _body_snippet(resp))

                # 429/503 retry on any method; 500/502/504 only where a
                # repeat is safe, because the request may already have run.
                if resp.status_code in _AMBIGUOUS_RETRY_CODES and not may_retry_after_send:
                    ambiguous = ServerError(resp.status_code, method, path, _body_snippet(resp))
                    # The reason goes in the hint, not the message: the
                    # message is the one line the CLI always prints, and
                    # the hint is where it puts the next step. Without it
                    # operator sees "HTTP 504" and reasonably assumes the
                    # request did nothing.
                    ambiguous.hint = (
                        f"The server sent {resp.status_code} after receiving the "
                        f"{method.upper()}, so it may have partially executed. It was "
                        "not retried automatically -- check server state before "
                        "repeating it."
                    )
                    raise ambiguous

                if attempt < client.max_retries:
                    # An explicit Retry-After is an instruction, so it is used
                    # verbatim; only our own backoff gets jitter.
                    retry_after = _retry_after_seconds(resp)
                    delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                    # WARNING, not DEBUG: a retry storm is the single most
                    # common cause of "xnatctl is hanging", and it used to
                    # be completely invisible.
                    logger.warning(
                        "HTTP %d on %s %s; retrying in %.1fs%s (attempt %d/%d)",
                        resp.status_code,
                        method.upper(),
                        path,
                        delay,
                        " per Retry-After" if retry_after is not None else "",
                        attempt + 1,
                        client.max_retries + 1,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise RetryExhaustedError("request", client.max_retries + 1, last_error)

            # Non-retryable error status: surface a typed error, never a raw
            # httpx.HTTPStatusError (401/403/404 are already handled above).
            if resp.status_code >= 400:
                _raise_for_xnat_status(client.base_url, resp, method, path)

            return resp

        except httpx.ConnectTimeout as e:
            # Fail fast: the connect phase timed out (host blackholed /
            # firewall-DROPped). A typed RequestTimeoutError instead of the generic
            # NetworkError bucket, and NOT retried -- an unreachable host will
            # not recover within the backoff window, and the whole point of
            # the split timeout is failing in seconds, not hours. (A future
            # revision may add
            # idempotency-aware connect retries.)
            raise XNATTimeoutError(client.base_url, DEFAULT_CONNECT_TIMEOUT_SECONDS) from e
        except httpx.ConnectError:
            # Connect phase: the request never reached the server, so a retry
            # cannot duplicate a side effect regardless of method.
            last_error = ServerUnreachableError(client.base_url)
        except httpx.TimeoutException as e:
            # Read/write/pool phase: the server HAS seen the request. Retrying
            # a non-idempotent method here risks executing it twice.
            if not may_retry_after_send:
                raise XNATTimeoutError(
                    client.base_url,
                    read_timeout,
                    f"{method.upper()} {path} timed out after the request was sent; "
                    "it may have partially executed on the server. Not retried "
                    "automatically - check server state before repeating it.",
                ) from e
            last_error = NetworkError(client.base_url, f"Timeout after {read_timeout}s")
        except PERMANENT_TRANSPORT_ERRORS as e:
            # Wrong scheme, malformed URL, undecodable body: retrying cannot
            # help, so fail now with a typed error rather than after backoff.
            raise NetworkError(client.base_url, f"{type(e).__name__}: {e}") from e
        except RETRYABLE_TRANSPORT_ERRORS as e:
            # Socket died mid-exchange / proxy hiccup. Same send-phase hazard
            # as a read timeout, so it obeys the same idempotency rule.
            if not may_retry_after_send:
                raise NetworkError(
                    client.base_url,
                    f"{type(e).__name__} on {method.upper()} {path} after the request "
                    "was sent; it may have partially executed on the server. "
                    "Not retried automatically.",
                ) from e
            last_error = NetworkError(client.base_url, f"{type(e).__name__}: {e}")
        except (AuthenticationError, ResourceNotFoundError):
            raise

        # Retry with full-jitter backoff.
        if attempt < client.max_retries:
            delay = _backoff_delay(attempt)
            logger.warning(
                "%s on %s %s; retrying in %.1fs (attempt %d/%d)",
                type(last_error).__name__ if last_error else "Transport error",
                method.upper(),
                path,
                delay,
                attempt + 1,
                client.max_retries + 1,
            )
            time.sleep(delay)

        attempt += 1

    raise RetryExhaustedError("request", client.max_retries + 1, last_error)
