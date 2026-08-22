"""HTTP client for XNAT REST API.

Provides retry logic, pagination, and session-based authentication.
"""

from __future__ import annotations

import logging
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from xnatctl.services.admin import AdminService
    from xnatctl.services.downloads import DownloadService
    from xnatctl.services.exam_upload import ExamUploadService
    from xnatctl.services.hierarchy import HierarchyService
    from xnatctl.services.pipelines import PipelineService
    from xnatctl.services.prearchive import PrearchiveService
    from xnatctl.services.projects import ProjectService
    from xnatctl.services.resources import ResourceService
    from xnatctl.services.scans import ScanService
    from xnatctl.services.sessions import SessionService
    from xnatctl.services.subjects import SubjectService
    from xnatctl.services.upload import UploadService

from xnatctl.core import transport
from xnatctl.core.exceptions import (
    AuthenticationError,
    NetworkError,
    PermissionDeniedError,
    ServerUnreachableError,
    SessionExpiredError,
)
from xnatctl.core.exceptions import (
    RequestTimeoutError as XNATTimeoutError,
)
from xnatctl.core.redact import redact_url_query
from xnatctl.core.retry import PERMANENT_TRANSPORT_ERRORS, RETRYABLE_TRANSPORT_ERRORS
from xnatctl.core.timeouts import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    build_httpx_timeout,
)
from xnatctl.core.validation import validate_server_url

# =============================================================================
# Constants
# =============================================================================

DEFAULT_TIMEOUT = DEFAULT_HTTP_TIMEOUT_SECONDS
DEFAULT_MAX_RETRIES = 3
_AUTH_LOGGED_IN_RE = re.compile(r"User '([^']+)' is logged in")

logger = logging.getLogger(__name__)

_ServiceT = TypeVar("_ServiceT")


# =============================================================================
# XNATClient
# =============================================================================


@dataclass
class XNATClient:
    """HTTP client for XNAT REST API with retry and pagination."""

    base_url: str
    username: str | None = None
    password: str | None = None
    session_token: str | None = None
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    verify_ssl: bool = True
    ca_bundle: str | None = None
    auto_reauth: bool = False
    # Injection seam for the underlying HTTP transport. Standard httpx practice
    # for library consumers, and what lets tests drive real request/response
    # cycles through httpx.MockTransport instead of mocking the client.
    transport: httpx.BaseTransport | None = None
    _client: httpx.Client | None = field(init=False, default=None, repr=False)
    _ssl_context: ssl.SSLContext | None = field(init=False, default=None, repr=False)
    # Lazily-built service objects, cached per client instance so repeated
    # ``client.projects`` access returns the same bound service.
    _services: dict[str, Any] = field(init=False, default_factory=dict, repr=False)
    # Serializes session refresh across parallel streams: without it, N workers
    # hitting one expiry each open their own fresh session, which is exactly
    # the concurrent-session exhaustion that locks out shared service accounts.
    _reauth_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Validate and normalize URL."""
        self.base_url = validate_server_url(self.base_url)

    @classmethod
    def from_profile(
        cls,
        name: str | None = None,
        *,
        config_path: Path | None = None,
    ) -> XNATClient:
        """Build a client from a config profile, resolving credentials.

        The one-call entry point for library use: it runs the same credential
        resolution the CLI does (env vars over profile config, cached/env
        session token, ``auto_reauth`` on). See
        :func:`xnatctl.core.connect.build_client_from_profile`.

        Args:
            name: Profile name. ``None`` uses the config default.
            config_path: Optional config file path.

        Returns:
            A ready-to-use XNATClient.

        Raises:
            ProfileNotFoundError: If the named profile does not exist.
        """
        # Function-local to avoid the core.connect -> core.client import cycle:
        # connect imports this module, so this module cannot import connect at
        # module scope.
        from xnatctl.core.connect import build_client_from_profile

        return build_client_from_profile(name, config_path=config_path)

    # =========================================================================
    # Client Management
    # =========================================================================

    def httpx_verify(self) -> ssl.SSLContext | bool:
        """TLS verification value for any httpx client that speaks for this one.

        The upload and fast-download paths build raw ``httpx.Client`` instances
        (per-thread, or to keep the upload retry ladder outside ``_request``);
        they call this so a ``ca_bundle`` profile carries its trust decisions
        there too instead of silently degrading to the bare bool. A custom CA
        bundle is a secure alternative to disabling verification for
        self-signed sites: build an SSLContext from it (httpx 0.28 deprecates
        passing a bare path as ``verify``). The context is built once and
        shared -- loading the bundle per call would also defeat the gradual
        path's per-thread client cache, which keys on this value.
        """
        if self.ca_bundle:
            # Unlocked memoization: callers grab this on the main thread before
            # spawning workers, and the worst race builds the context twice.
            if self._ssl_context is None:
                self._ssl_context = ssl.create_default_context(cafile=self.ca_bundle)
            return self._ssl_context
        return self.verify_ssl

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            if not self.verify_ssl and not self.ca_bundle:
                logger.warning(
                    "TLS certificate verification is DISABLED for %s",
                    redact_url_query(self.base_url),
                )
            self._client = httpx.Client(
                base_url=self.base_url,
                # Structured timeout: a short connect phase so a
                # blackholed host fails in seconds, with the long read ceiling
                # preserved for large transfers.
                timeout=build_httpx_timeout(self.timeout),
                verify=self.httpx_verify(),
                follow_redirects=True,
                transport=self.transport,
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> XNATClient:
        # Authenticate on entry only when a password login is both possible and
        # needed: credentials present and no token yet. An env or cached token
        # already authenticates the client, so entering must not spend a login
        # round-trip -- and a token-only client (no password) has nothing to log
        # in with.
        if self.session_token is None and self.username and self.password:
            self.authenticate()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # =========================================================================
    # Service Accessors
    # =========================================================================

    def _service(self, name: str, factory: Callable[[XNATClient], _ServiceT]) -> _ServiceT:
        """Return the cached service for ``name``, building it once on first use.

        The service imports are function-local at each property to avoid the
        core -> services import cycle (services import core.client); this just
        holds the per-instance cache so repeated access returns one object.
        """
        service = self._services.get(name)
        if service is None:
            service = factory(self)
            self._services[name] = service
        return cast("_ServiceT", service)

    @property
    def projects(self) -> ProjectService:
        """Bound :class:`ProjectService` for this client (cached)."""
        from xnatctl.services.projects import ProjectService

        return self._service("projects", ProjectService)

    @property
    def subjects(self) -> SubjectService:
        """Bound :class:`SubjectService` for this client (cached)."""
        from xnatctl.services.subjects import SubjectService

        return self._service("subjects", SubjectService)

    @property
    def sessions(self) -> SessionService:
        """Bound :class:`SessionService` for this client (cached)."""
        from xnatctl.services.sessions import SessionService

        return self._service("sessions", SessionService)

    @property
    def scans(self) -> ScanService:
        """Bound :class:`ScanService` for this client (cached)."""
        from xnatctl.services.scans import ScanService

        return self._service("scans", ScanService)

    @property
    def resources(self) -> ResourceService:
        """Bound :class:`ResourceService` for this client (cached)."""
        from xnatctl.services.resources import ResourceService

        return self._service("resources", ResourceService)

    @property
    def prearchive(self) -> PrearchiveService:
        """Bound :class:`PrearchiveService` for this client (cached)."""
        from xnatctl.services.prearchive import PrearchiveService

        return self._service("prearchive", PrearchiveService)

    @property
    def pipelines(self) -> PipelineService:
        """Bound :class:`PipelineService` for this client (cached)."""
        from xnatctl.services.pipelines import PipelineService

        return self._service("pipelines", PipelineService)

    @property
    def admin(self) -> AdminService:
        """Bound :class:`AdminService` for this client (cached)."""
        from xnatctl.services.admin import AdminService

        return self._service("admin", AdminService)

    @property
    def hierarchy(self) -> HierarchyService:
        """Bound :class:`HierarchyService` for this client (cached)."""
        from xnatctl.services.hierarchy import HierarchyService

        return self._service("hierarchy", HierarchyService)

    @property
    def downloads(self) -> DownloadService:
        """Bound :class:`DownloadService` for this client (cached)."""
        from xnatctl.services.downloads import DownloadService

        return self._service("downloads", DownloadService)

    @property
    def uploads(self) -> UploadService:
        """Bound :class:`UploadService` for this client (cached)."""
        from xnatctl.services.upload import UploadService

        return self._service("uploads", UploadService)

    @property
    def exam_uploads(self) -> ExamUploadService:
        """Bound :class:`ExamUploadService` for this client (cached)."""
        from xnatctl.services.exam_upload import ExamUploadService

        return self._service("exam_uploads", ExamUploadService)

    # =========================================================================
    # Authentication
    # =========================================================================

    @property
    def is_authenticated(self) -> bool:
        """Check if client has a session token."""
        return self.session_token is not None

    def authenticate(self) -> str:
        """Authenticate with username/password and get JSESSIONID.

        Returns:
            Session token (JSESSIONID).

        Raises:
            AuthenticationError: If authentication fails.
        """
        if not self.username or not self.password:
            raise AuthenticationError(self.base_url, "Username and password required")

        client = self._get_client()

        try:
            resp = client.post(
                "/data/JSESSION",
                auth=(self.username, self.password),
            )
        except httpx.ConnectTimeout as e:
            # Connect-phase timeout: same typed, fail-fast contract as _request.
            raise XNATTimeoutError(self.base_url, DEFAULT_CONNECT_TIMEOUT_SECONDS) from e
        except httpx.ConnectError as e:
            raise ServerUnreachableError(self.base_url) from e
        except httpx.TimeoutException as e:
            raise NetworkError(self.base_url, f"Timeout: {e}") from e
        except (*RETRYABLE_TRANSPORT_ERRORS, *PERMANENT_TRANSPORT_ERRORS) as e:
            # Same contract as _request: no raw httpx error escapes. Login is a
            # single attempt, so transient and permanent collapse to one branch.
            raise NetworkError(self.base_url, f"{type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            raise AuthenticationError(self.base_url, f"HTTP {resp.status_code}")

        # XNAT returns HTML on auth failure
        if "<html" in resp.text.lower():
            raise AuthenticationError(self.base_url, "Invalid credentials or password expired")

        self.session_token = resp.text.strip()
        # Never the token itself -- only that one was obtained.
        logger.debug("Authenticated as %s at %s", self.username, redact_url_query(self.base_url))
        return self.session_token

    def invalidate_session(self) -> None:
        """Logout and clear session token."""
        if self.session_token:
            try:
                client = self._get_client()
                # Cookie on the client instance, not per-request: httpx 0.28
                # deprecates `cookies=` on individual calls.
                client.cookies.set("JSESSIONID", self.session_token)
                client.delete("/data/JSESSION")
            except Exception:
                pass  # Best effort
            finally:
                self.session_token = None
                # Do not leave the dead credential sitting on the client.
                if self._client is not None:
                    self._client.cookies.delete("JSESSIONID")

    # =========================================================================
    # HTTP Methods
    # =========================================================================

    def _get_cookies(self) -> dict[str, str]:
        """Get cookies for request."""
        if self.session_token:
            return {"JSESSIONID": self.session_token}
        return {}

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
    ) -> AbstractContextManager[httpx.Response]:
        """Stream a response through the client's retry/auth/error contract.

        The public streaming entry point every download path uses instead of
        reaching for ``_get_client()`` and raw ``httpx`` -- so streamed reads
        get the same retry ladder, typed-error mapping, and basic-auth fallback
        as ``_request``.

        Retries (retryable statuses, connect drops, and read-phase failures for
        idempotent methods) happen ONLY before the body is yielded; a mid-body
        transport failure is translated to the matching typed error but never
        retried, because a consumed stream cannot be resumed. Error statuses
        are mapped to the same typed exceptions ``_request`` raises. No raw
        ``httpx`` exception escapes.

        Args:
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
                failures, mirroring ``_request``.
            RetryExhaustedError: when retries drain.
        """
        return transport.stream(self, method, path, params=params, headers=headers, timeout=timeout)

    def _request(
        self,
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
        """Execute HTTP request with retry logic.

        Args:
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
        return transport.request(
            self,
            method,
            path,
            params=params,
            json=json,
            data=data,
            content=content,
            files=files,
            headers=headers,
            timeout=timeout,
            retry_non_idempotent=retry_non_idempotent,
        )

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> httpx.Response:
        """GET request."""
        return self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout=timeout,
        )

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        content: Any | None = None,
        files: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> httpx.Response:
        """POST request."""
        return self._request(
            "POST",
            path,
            params=params,
            json=json,
            data=data,
            content=content,
            files=files,
            headers=headers,
            timeout=timeout,
        )

    def put(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        content: Any | None = None,
        files: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> httpx.Response:
        """PUT request."""
        return self._request(
            "PUT",
            path,
            params=params,
            json=json,
            data=data,
            content=content,
            files=files,
            headers=headers,
            timeout=timeout,
        )

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> httpx.Response:
        """DELETE request."""
        return self._request(
            "DELETE",
            path,
            params=params,
            headers=headers,
            timeout=timeout,
        )

    # =========================================================================
    # Pagination
    # =========================================================================

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        result_key: str = "ResultSet.Result",
    ) -> Iterator[dict[str, Any]]:
        """Paginated GET returning items one by one.

        Args:
            path: API path.
            params: Additional query parameters.
            page_size: Number of items per page.
            result_key: Dot-separated path to results in response.

        Yields:
            Individual result items.
        """
        offset = 0
        base_params = params.copy() if params else {}
        base_params["format"] = "json"
        previous_page: list[Any] | None = None

        while True:
            page_params = {
                **base_params,
                "offset": offset,
                "limit": page_size,
            }

            resp = self.get(path, params=page_params)
            data = resp.json()

            # Navigate to results using dot notation
            results = data
            for key in result_key.split("."):
                results = results.get(key, []) if isinstance(results, dict) else []

            # Per-page visibility turns "the list command is slow" into a
            # number of pages and items. Logged in the client rather
            # than BaseService._paginate, which only delegates here.
            logger.debug(
                "paginate %s: offset=%d limit=%d -> %d items",
                path,
                offset,
                page_size,
                len(results),
            )

            if not results:
                break

            # Not every XNAT endpoint paginates. XNAT 1.9.2.1 ignores `limit`
            # and `offset` on /data/projects entirely and answers every
            # request with the full result set -- so the loop below advanced
            # the offset forever, re-yielding the same rows, and callers hung.
            # Verified against a live server; the integration tier reached
            # offset 151450 before it was stopped.
            #
            # Two shapes of that, and the first alone is not enough: a server
            # returning more rows than `limit` asked for is obviously not
            # honouring it, but when the collection is smaller than a page
            # the counts look perfectly normal and only the repetition shows
            # it. Both are checked before the offset advances.
            ignores_limit = len(results) > page_size
            repeats_page = results == previous_page
            if ignores_limit or repeats_page:
                if not repeats_page:
                    yield from results
                # WARNING, not DEBUG. Stopping is right for a server that
                # ignores these parameters, but it is indistinguishable from
                # one that honours `limit` while ignoring `offset` -- where
                # stopping truncates the listing instead. Nobody runs this at
                # DEBUG, and a short result set that should have been long is
                # exactly the kind of wrong answer that gets believed.
                logger.warning(
                    "paginate %s: %s at offset=%d limit=%d. Treating the endpoint as "
                    "unpaginated and stopping after %d row(s); if the server does "
                    "paginate, this listing may be incomplete.",
                    path,
                    "returned more rows than requested" if ignores_limit else "repeated a page",
                    offset,
                    page_size,
                    len(results),
                )
                break

            yield from results
            previous_page = results
            offset += page_size

            if len(results) < page_size:
                break

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET request returning JSON."""
        if params is None:
            params = {}
        params["format"] = "json"
        resp = self.get(path, params=params)
        return resp.json()

    def ping(self) -> dict[str, Any]:
        """Check server connectivity and get version info.

        Returns:
            Dict with server info.

        Raises:
            NetworkError: If server is unreachable.
        """
        start = time.time()
        resp = self.get("/xapi/siteConfig/buildInfo/version")
        latency = int((time.time() - start) * 1000)

        return {
            "url": self.base_url,
            "status": "ok",
            "version": resp.text.strip(),
            "latency_ms": latency,
        }

    def whoami(self) -> dict[str, Any]:
        """Get current user information.

        Returns:
            Dict with user info.

        Raises:
            AuthenticationError: If not authenticated.
        """
        current_username = self._get_current_username()
        if current_username:
            display_username = self._apply_username_hint(current_username)
            details = self._get_user_details(current_username)
            if details is not None:
                details["username"] = display_username
                return details
            return {
                "username": display_username,
                "firstname": "",
                "lastname": "",
                "email": "",
                "enabled": True,
            }

        if self.username:
            return {
                "username": self.username,
                "firstname": "",
                "lastname": "",
                "email": "",
                "enabled": True,
            }

        return {
            "username": "unknown",
            "firstname": "",
            "lastname": "",
            "email": "",
            "enabled": False,
        }

    def _get_current_username(self) -> str | None:
        """Resolve the authenticated username from server endpoints.

        Some XNAT deployments return a full user listing from `/data/user`,
        which is not a reliable whoami source. Prefer dedicated current-user
        endpoints when available.
        """
        try:
            resp = self.get("/xapi/users/username")
            username = resp.text.strip()
            if username and "<html" not in username.lower():
                return username
        except (AuthenticationError, SessionExpiredError, PermissionDeniedError):
            raise
        except Exception:
            pass

        try:
            resp = self.get("/data/auth")
            match = _AUTH_LOGGED_IN_RE.search(resp.text)
            if match:
                return match.group(1).strip()
        except (AuthenticationError, SessionExpiredError, PermissionDeniedError):
            raise
        except Exception:
            pass

        return None

    def _get_user_details(self, username: str) -> dict[str, Any] | None:
        """Fetch user details for a resolved username, if available."""
        try:
            data = self.get_json(f"/xapi/users/{quote(username, safe='')}")
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        return {
            "username": data.get("username", username),
            "firstname": data.get("firstName", ""),
            "lastname": data.get("lastName", ""),
            "email": data.get("email", ""),
            "enabled": data.get("enabled", False),
        }

    def _apply_username_hint(self, username: str) -> str:
        """Preserve configured/cached username casing when it matches."""
        if self.username and self.username.casefold() == username.casefold():
            return self.username
        return username
