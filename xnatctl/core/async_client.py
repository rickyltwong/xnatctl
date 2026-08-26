"""Async HTTP client for the XNAT REST API -- read path only.

:class:`AsyncXNATClient` is the async twin of
:class:`xnatctl.core.client.XNATClient`, built over ``httpx.AsyncClient``
instead of ``httpx.Client``. It shares the sync client's retry policy,
status->exception mapping, and typed exception hierarchy exactly -- a caller
can ``except SessionExpiredError`` (or any other
:class:`~xnatctl.core.exceptions.XNATCtlError` subclass) identically whether
it came from the sync or the async client. See
:mod:`xnatctl.core.async_transport` for how that sharing works.

**Read path only.** Uploads and downloads are not implemented here and are
not planned for this class -- they stay on :class:`XNATClient`. Only ``get``,
``get_json``, and ``stream`` are exposed; there is no async ``post``/``put``/
``delete``. There are also no async service accessors (no ``client.projects``,
``client.sessions``, ...): every existing service class wraps a *sync*
``BaseService._get``/``_post`` that would block the event loop if called from
here, and re-deriving each service's XNAT-specific routing/parsing logic in a
parallel async module would drift from the real one.

**Semver**: Provisional, not part of ``xnatctl.__all__``. See
``docs/stability.rst``.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from xnatctl.core import async_transport
from xnatctl.core.client import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from xnatctl.core.exceptions import (
    AuthenticationError,
    NetworkError,
    ServerUnreachableError,
)
from xnatctl.core.exceptions import (
    RequestTimeoutError as XNATTimeoutError,
)
from xnatctl.core.redact import redact_url_query
from xnatctl.core.retry import PERMANENT_TRANSPORT_ERRORS, RETRYABLE_TRANSPORT_ERRORS
from xnatctl.core.timeouts import DEFAULT_CONNECT_TIMEOUT_SECONDS, build_httpx_timeout
from xnatctl.core.validation import validate_server_url

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AsyncXNATClient:
    """Async HTTP client for XNAT REST reads, with retry and session management.

    Field-for-field the same shape as :class:`~xnatctl.core.client.XNATClient`
    (see that class for the meaning of each), except ``transport`` takes an
    ``httpx.AsyncBaseTransport`` (the injection seam ``httpx.MockTransport``
    also supports for async handlers) and the reauth lock is an
    ``asyncio.Lock`` rather than a ``threading.Lock``.
    """

    base_url: str
    username: str | None = None
    password: str | None = None
    session_token: str | None = None
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    verify_ssl: bool = True
    ca_bundle: str | None = None
    auto_reauth: bool = False
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(init=False, default=None, repr=False)
    _ssl_context: ssl.SSLContext | None = field(init=False, default=None, repr=False)
    # asyncio.Lock() does not need a running loop at construction time (true
    # since Python 3.10); it binds lazily on first `await`, so building one
    # here as a dataclass field default is safe under this project's
    # `requires-python = ">=3.11"` floor.
    _reauth_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False)
    # Guards first-time construction of ``_client``/``_ssl_context`` in
    # ``_get_client``. Building the SSLContext for a ``ca_bundle`` profile
    # requires an ``await`` (the file read is pushed off the loop), which
    # opens a window between the "not built yet" check and the assignment --
    # without this lock, two concurrent calls could each build and assign
    # their own ``httpx.AsyncClient``, leaking one's connection pool.
    _client_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        """Validate and normalize URL."""
        self.base_url = validate_server_url(self.base_url)

    @classmethod
    async def from_profile(
        cls,
        name: str | None = None,
        *,
        config_path: Path | None = None,
    ) -> AsyncXNATClient:
        """Build a client from a config profile, resolving credentials.

        Async twin of :meth:`XNATClient.from_profile
        <xnatctl.core.client.XNATClient.from_profile>`: reuses
        :func:`xnatctl.core.connect.resolve_client_params` unmodified. That
        function is pure credential resolution (env vars, profile config, the
        cached session file) with no client construction inside it, so it
        produces the same kwargs dict for either client class.

        This is a coroutine, not a plain classmethod, precisely because that
        resolution is not pure computation: ``Config.load`` reads
        ``config.yaml``, ``AuthManager.load_session`` reads (and rewrites) the
        session-cache file, and a ``password_source: keyring`` profile calls
        into the OS keychain -- all synchronous, blocking I/O. This class
        exists for callers (Airflow deferrable operators are the motivating
        case) that share one event loop across many concurrent operations, so
        those calls run on a worker thread via ``asyncio.to_thread`` instead
        of stalling every other coroutine on the loop for the duration of a
        disk read or a keychain prompt.

        Args:
            name: Profile name. ``None`` uses the config default.
            config_path: Optional config file path.

        Returns:
            A ready-to-use AsyncXNATClient (not yet authenticated).

        Raises:
            ProfileNotFoundError: If the named profile does not exist.
        """
        # Deferred: tests monkeypatch xnatctl.core.connect.resolve_client_params
        # to intercept the lookup; a module-scope import would bind the real
        # function before the patch runs (see tests/test_async_client.py).
        from xnatctl.core.connect import resolve_client_params

        params = await asyncio.to_thread(resolve_client_params, name, config_path=config_path)
        return cls(**params)

    # =========================================================================
    # Client Management
    # =========================================================================

    async def httpx_verify(self) -> ssl.SSLContext | bool:
        """TLS verification value for the underlying ``httpx.AsyncClient``.

        Logic identical to :meth:`XNATClient.httpx_verify
        <xnatctl.core.client.XNATClient.httpx_verify>`, but async: building an
        ``SSLContext`` -- for a ``ca_bundle`` profile *or* the default
        ``verify_ssl=True`` path -- reads from disk (an explicit CA bundle
        file, or the certifi bundle ``httpx.create_ssl_context`` loads by
        default), so that read runs on a worker thread via
        ``asyncio.to_thread`` rather than blocking the loop. Passing a plain
        ``verify=True`` bool straight into ``httpx.AsyncClient(...)`` would
        let *it* build the default context synchronously at construction
        time, which is exactly the blocking call this method exists to avoid
        -- so the default path is resolved to a concrete ``SSLContext`` here
        too, not deferred to httpx. Sharing the sync version's body across an
        ``await`` boundary is not possible without duplicating it, for the
        same reason as the retry ladder itself: a function cannot be shared
        between a sync and an async caller without a code-generation step.

        ``ca_bundle`` is checked BEFORE ``verify_ssl``, matching the sync
        client exactly: a CA bundle is the secure alternative to disabling
        verification, so ``verify_ssl: false`` plus a ``ca_bundle`` must
        still verify against that bundle, not silently skip verification.
        Checking ``verify_ssl`` first would return ``False`` before
        ``ca_bundle`` was ever considered.
        """
        if self.ca_bundle:
            # Unlocked memoization, matching the sync client: callers that
            # care about the race build the context on the main thread
            # before spawning workers, and the worst case here just builds
            # it twice.
            if self._ssl_context is None:
                self._ssl_context = await asyncio.to_thread(
                    ssl.create_default_context, cafile=self.ca_bundle
                )
            return self._ssl_context
        if not self.verify_ssl:
            return False
        if self._ssl_context is None:
            # Delegates to httpx's own default-context construction
            # (certifi bundle, SSL_CERT_FILE/SSL_CERT_DIR env overrides)
            # instead of reimplementing it, so this stays identical to
            # what ``httpx.AsyncClient(verify=True)`` would have built
            # inline -- just off the loop.
            self._ssl_context = await asyncio.to_thread(httpx.create_ssl_context)
        return self._ssl_context

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the underlying ``httpx.AsyncClient``.

        Guarded by ``_client_lock``: constructing the client involves an
        ``await`` (``httpx_verify``, which may read a CA bundle off the loop),
        so without the lock two requests arriving before the first client
        exists could each build and assign their own, leaking a connection
        pool. The common case -- the client already exists -- returns before
        ever touching the lock.
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                if not self.verify_ssl and not self.ca_bundle:
                    logger.warning(
                        "TLS certificate verification is DISABLED for %s",
                        redact_url_query(self.base_url),
                    )
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=build_httpx_timeout(self.timeout),
                    verify=await self.httpx_verify(),
                    follow_redirects=True,
                    transport=self.transport,
                )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> AsyncXNATClient:
        # Same condition as XNATClient.__enter__: authenticate only when a
        # password login is both possible and needed.
        if self.session_token is None and self.username and self.password:
            await self.authenticate()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    # =========================================================================
    # Authentication
    # =========================================================================

    @property
    def is_authenticated(self) -> bool:
        """Check if client has a session token."""
        return self.session_token is not None

    async def authenticate(self) -> str:
        """Authenticate with username/password and get JSESSIONID.

        Async twin of :meth:`XNATClient.authenticate
        <xnatctl.core.client.XNATClient.authenticate>`, duplicated rather than
        shared for the same reason as the retry ladder itself: an ``await``
        cannot be inserted into a function shared with a sync caller without
        a code-generation step this class of change does not justify.

        Returns:
            Session token (JSESSIONID).

        Raises:
            AuthenticationError: If authentication fails.
        """
        if not self.username or not self.password:
            raise AuthenticationError(self.base_url, "Username and password required")

        client = await self._get_client()

        try:
            resp = await client.post(
                "/data/JSESSION",
                auth=(self.username, self.password),
            )
        except httpx.ConnectTimeout as e:
            raise XNATTimeoutError(self.base_url, DEFAULT_CONNECT_TIMEOUT_SECONDS) from e
        except httpx.ConnectError as e:
            raise ServerUnreachableError(self.base_url) from e
        except httpx.TimeoutException as e:
            raise NetworkError(self.base_url, f"Timeout: {e}") from e
        except (*RETRYABLE_TRANSPORT_ERRORS, *PERMANENT_TRANSPORT_ERRORS) as e:
            raise NetworkError(self.base_url, f"{type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            raise AuthenticationError(self.base_url, f"HTTP {resp.status_code}")

        # XNAT returns HTML on auth failure
        if "<html" in resp.text.lower():
            raise AuthenticationError(self.base_url, "Invalid credentials or password expired")

        self.session_token = resp.text.strip()
        logger.debug("Authenticated as %s at %s", self.username, redact_url_query(self.base_url))
        return self.session_token

    # =========================================================================
    # HTTP Methods (read path only)
    # =========================================================================

    def _get_auth(self) -> tuple[str, str] | None:
        """Get basic auth tuple if no session token."""
        if not self.session_token and self.username and self.password:
            return (self.username, self.password)
        return None

    def stream(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> AbstractAsyncContextManager[httpx.Response]:
        """Stream a response through the client's retry/auth/error contract.

        Async twin of :meth:`XNATClient.stream
        <xnatctl.core.client.XNATClient.stream>` -- same contract, same typed
        exceptions, ``async with`` instead of ``with``.

        Args:
            method: HTTP method (GET in practice).
            path: API path.
            params: Query parameters.
            headers: Additional request headers.
            timeout: Read-timeout override in seconds.

        Yields:
            The open streaming response (2xx). Closed on context exit.

        Raises:
            SessionExpiredError, PermissionDeniedError, ResourceNotFoundError,
            ClientRequestError, ServerError: mapped from the error status.
            RequestTimeoutError, NetworkError, ServerUnreachableError: from
                transport failures.
            RetryExhaustedError: when retries drain.
        """
        return async_transport.stream(
            self, method, path, params=params, headers=headers, timeout=timeout
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request with retry logic. See :func:`async_transport.request`."""
        return await async_transport.request(
            self,
            method,
            path,
            params=params,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
        )

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        """GET request.

        ``max_retries`` overrides the client's retry budget for this call
        alone; pass 0 to make it single-shot.
        """
        return await self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
        )

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET request returning JSON."""
        if params is None:
            params = {}
        params["format"] = "json"
        resp = await self.get(path, params=params)
        return resp.json()
