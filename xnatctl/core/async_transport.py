"""Async retry-ladder HTTP transport backing :class:`AsyncXNATClient`.

The async twin of :mod:`xnatctl.core.transport`. It does NOT re-derive the
retry/error policy: the status-code sets, transport-failure taxonomy,
backoff/Retry-After math, and status->exception mapping already live as
plain functions in :mod:`xnatctl.core.transport` and :mod:`xnatctl.core.retry`
that take no ``httpx.Client`` reference, so they are imported here unchanged.
What is duplicated instead of shared is the orchestration loop itself
(``request``/``stream``), which
has no portable sync/async-agnostic form in plain Python, so it is
re-implemented here with ``await``/``asyncio.sleep``/``asyncio.Lock`` in
place of the sync client's blocking calls, kept line-for-line comparable to
its sync twin.

Read path only: nothing in this module is called for uploads or downloads,
which stay sync-only. See :class:`xnatctl.core.async_client.AsyncXNATClient`
for the public contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx

from xnatctl.core.exceptions import (
    AuthenticationError,
    NetworkError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
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
from xnatctl.core.transport import (
    _STREAM_TRANSPORT_ERRORS,
    _body_snippet,
    _raise_for_xnat_status,
    _stream_transport_error,
)

if TYPE_CHECKING:
    from xnatctl.core.async_client import AsyncXNATClient

logger = logging.getLogger(__name__)


async def _aread_stream_body(resp: httpx.Response) -> None:
    """Buffer a streaming response's body so ``.text`` is available for errors.

    Async twin of :func:`xnatctl.core.transport._read_stream_body`: the sync
    version's ``resp.read()`` is not valid on a response opened through
    ``httpx.AsyncClient``, which requires ``await resp.aread()`` instead.
    """
    try:
        await resp.aread()
    except Exception:  # noqa: BLE001 -- a truncated error body must not eclipse the status it describes
        pass


@asynccontextmanager
async def stream(
    client: AsyncXNATClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
) -> AsyncIterator[httpx.Response]:
    """Implementation behind :meth:`AsyncXNATClient.stream`.

    Mirrors :func:`xnatctl.core.transport.stream` exactly -- same retry
    ladder, same status->exception mapping, same "retries only before the
    body is yielded, never mid-body" contract -- substituting
    ``asyncio.sleep``/``asyncio.Lock``/``await`` for the sync client's
    blocking equivalents. See that function's docstring for the full
    documented contract; it applies here unchanged.
    """
    http_client = await client._get_client()
    read_timeout = timeout or client.timeout
    request_timeout = build_httpx_timeout(read_timeout)
    method_upper = method.upper()
    may_retry_after_send = method_upper in IDEMPOTENT_METHODS
    last_error: Exception | None = None
    did_reauth = False

    attempt = 0
    while attempt <= client.max_retries:
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
            response = await http_client.send(request, auth=auth, stream=True)
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
                "%s %s -> %d (async stream, attempt %d/%d)",
                method_upper,
                redact_url_query(str(response.request.url)),
                code,
                attempt + 1,
                client.max_retries + 1,
                extra={
                    "event": "http_request",
                    "method": method_upper,
                    "status": code,
                    "attempt": attempt + 1,
                },
            )

            can_reauth = (
                client.auto_reauth and not did_reauth and bool(client.username and client.password)
            )
            if code == 401 and can_reauth:
                await response.aclose()
                async with client._reauth_lock:
                    # A parallel task may have refreshed the session while
                    # this request was in flight; reuse its token instead of
                    # opening yet another server session.
                    if client.session_token == token_at_send:
                        await client.authenticate()
                did_reauth = True
                continue

            if code in RETRYABLE_STATUS_CODES:
                await _aread_stream_body(response)
                last_error = ServerError(code, method, path, _body_snippet(response))

                if code in _AMBIGUOUS_RETRY_CODES and not may_retry_after_send:
                    await response.aclose()
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
                    await response.aclose()
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
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                await response.aclose()
                raise RetryExhaustedError("request", client.max_retries + 1, last_error)

            if code >= 400:
                # Terminal error: read the body so the typed exception can
                # quote it, then map and raise through the shared helper.
                await _aread_stream_body(response)
                try:
                    _raise_for_xnat_status(client.base_url, response, method, path)
                finally:
                    await response.aclose()

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
                await response.aclose()
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
            await asyncio.sleep(delay)
        attempt += 1

    raise RetryExhaustedError("request", client.max_retries + 1, last_error)


async def request(  # noqa: C901 -- mirrors xnatctl.core.transport.request; see that function's own noqa
    client: AsyncXNATClient,
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
    max_retries: int | None = None,
) -> httpx.Response:
    """Implementation behind :meth:`AsyncXNATClient.get` (and friends).

    Mirrors :func:`xnatctl.core.transport.request` -- same retry ladder,
    same status->exception mapping -- substituted for ``httpx.AsyncClient``.
    Only GET is exposed on :class:`AsyncXNATClient` in this pass, but this
    function stays method-generic like its sync twin so a future write path
    does not need a second ladder.
    """
    http_client = await client._get_client()
    retry_budget = client.max_retries if max_retries is None else max_retries

    read_timeout = timeout or client.timeout
    request_timeout = build_httpx_timeout(read_timeout)
    last_error: Exception | None = None
    did_reauth = False
    may_retry_after_send = method.upper() in IDEMPOTENT_METHODS or retry_non_idempotent

    attempt = 0
    while attempt <= retry_budget:
        # Captured before the request goes out so the 401 handler below can
        # tell "my token was the one that got rejected" from "a concurrent
        # task already refreshed it while I was in flight" -- same token-at-
        # send recheck the stream() ladder uses.
        token_at_send = client.session_token
        # Set the session cookie on the client instance rather than passing
        # ``cookies=`` per request (httpx 0.28 deprecates per-request
        # cookies), matching the sync ladder.
        if client.session_token:
            http_client.cookies.set("JSESSIONID", client.session_token)
        else:
            http_client.cookies.delete("JSESSIONID")
        auth = client._get_auth()
        started = time.monotonic()
        try:
            resp = await http_client.request(
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

            duration_ms = round((time.monotonic() - started) * 1000)
            logger.debug(
                "%s %s -> %d in %dms (attempt %d/%d)",
                method.upper(),
                redact_url_query(str(resp.request.url)),
                resp.status_code,
                duration_ms,
                attempt + 1,
                retry_budget + 1,
                extra={
                    "event": "http_request",
                    "method": method.upper(),
                    "status": resp.status_code,
                    "attempt": attempt + 1,
                    "duration_ms": duration_ms,
                },
            )

            if resp.status_code == 401:
                can_reauth = (
                    client.auto_reauth
                    and not did_reauth
                    and bool(client.username and client.password)
                )
                if can_reauth:
                    logger.debug("401 on %s %s; re-authenticating and retrying once", method, path)
                    async with client._reauth_lock:
                        # A parallel task may have refreshed the session
                        # while this request was in flight; reuse its token
                        # instead of opening yet another server session --
                        # same collapse-to-one-login behaviour as stream().
                        if client.session_token == token_at_send:
                            await client.authenticate()
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

            if resp.status_code in RETRYABLE_STATUS_CODES:
                last_error = ServerError(resp.status_code, method, path, _body_snippet(resp))

                if resp.status_code in _AMBIGUOUS_RETRY_CODES and not may_retry_after_send:
                    ambiguous = ServerError(resp.status_code, method, path, _body_snippet(resp))
                    ambiguous.hint = (
                        f"The server sent {resp.status_code} after receiving the "
                        f"{method.upper()}, so it may have partially executed. It was "
                        "not retried automatically -- check server state before "
                        "repeating it."
                    )
                    raise ambiguous

                if attempt < retry_budget:
                    retry_after = _retry_after_seconds(resp)
                    delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                    logger.warning(
                        "HTTP %d on %s %s; retrying in %.1fs%s (attempt %d/%d)",
                        resp.status_code,
                        method.upper(),
                        path,
                        delay,
                        " per Retry-After" if retry_after is not None else "",
                        attempt + 1,
                        retry_budget + 1,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise RetryExhaustedError("request", retry_budget + 1, last_error)

            if resp.status_code >= 400:
                _raise_for_xnat_status(client.base_url, resp, method, path)

            return resp

        except httpx.ConnectTimeout as e:
            raise XNATTimeoutError(client.base_url, DEFAULT_CONNECT_TIMEOUT_SECONDS) from e
        except httpx.ConnectError:
            last_error = ServerUnreachableError(client.base_url)
        except httpx.TimeoutException as e:
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
            raise NetworkError(client.base_url, f"{type(e).__name__}: {e}") from e
        except RETRYABLE_TRANSPORT_ERRORS as e:
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
        if attempt < retry_budget:
            delay = _backoff_delay(attempt)
            logger.warning(
                "%s on %s %s; retrying in %.1fs (attempt %d/%d)",
                type(last_error).__name__ if last_error else "Transport error",
                method.upper(),
                path,
                delay,
                attempt + 1,
                retry_budget + 1,
            )
            await asyncio.sleep(delay)

        attempt += 1

    raise RetryExhaustedError("request", retry_budget + 1, last_error)
