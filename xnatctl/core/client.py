"""HTTP client for XNAT REST API.

Provides retry logic, pagination, and session-based authentication.
"""

from __future__ import annotations

import logging
import re
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

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
from xnatctl.core.redact import redact_url_query
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.core.validation import validate_server_url

# =============================================================================
# Constants
# =============================================================================

DEFAULT_TIMEOUT = DEFAULT_HTTP_TIMEOUT_SECONDS
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
# Statuses safe to retry on the core client path. 429/500 join the original
# 502/503/504 (ROB-03); 400 stays upload-only (it encodes a transient XNAT
# import-race quirk handled in services/uploads.py), so it is NOT listed here.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRY_AFTER_STATUS_CODES = {429, 503}
_MAX_RETRY_AFTER_SECONDS = 300
_BODY_SNIPPET_CHARS = 200
_AUTH_LOGGED_IN_RE = re.compile(r"User '([^']+)' is logged in")

logger = logging.getLogger(__name__)


def _body_snippet(resp: httpx.Response) -> str:
    """Return a short, redacted snippet of a response body for error details."""
    try:
        return redact_url_query(resp.text[:_BODY_SNIPPET_CHARS])
    except Exception:
        return ""


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Return a bounded Retry-After delay (seconds) if the header is a usable int."""
    if resp.status_code not in _RETRY_AFTER_STATUS_CODES:
        return None
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(raw.strip())
    except (ValueError, AttributeError):
        return None
    if 0 <= seconds <= _MAX_RETRY_AFTER_SECONDS:
        return float(seconds)
    return None


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
    _client: httpx.Client | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate and normalize URL."""
        self.base_url = validate_server_url(self.base_url)

    # =========================================================================
    # Client Management
    # =========================================================================

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            # A custom CA bundle is a secure alternative to disabling
            # verification for self-signed sites. Build an SSLContext from it
            # (httpx 0.28 deprecates passing a bare path as ``verify``).
            verify: ssl.SSLContext | bool
            if self.ca_bundle:
                verify = ssl.create_default_context(cafile=self.ca_bundle)
            else:
                verify = self.verify_ssl
                if not self.verify_ssl:
                    logger.warning(
                        "TLS certificate verification is DISABLED for %s",
                        redact_url_query(self.base_url),
                    )
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                verify=verify,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> XNATClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

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
        except httpx.ConnectError as e:
            raise ServerUnreachableError(self.base_url) from e
        except httpx.TimeoutException as e:
            raise NetworkError(self.base_url, f"Timeout: {e}") from e

        if resp.status_code != 200:
            raise AuthenticationError(self.base_url, f"HTTP {resp.status_code}")

        # XNAT returns HTML on auth failure
        if "<html" in resp.text.lower():
            raise AuthenticationError(self.base_url, "Invalid credentials or password expired")

        self.session_token = resp.text.strip()
        return self.session_token

    def invalidate_session(self) -> None:
        """Logout and clear session token."""
        if self.session_token:
            try:
                client = self._get_client()
                client.delete(
                    "/data/JSESSION",
                    cookies={"JSESSIONID": self.session_token},
                )
            except Exception:
                pass  # Best effort
            finally:
                self.session_token = None

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

        Returns:
            HTTP response (2xx only; error statuses raise typed exceptions).

        Raises:
            SessionExpiredError: On 401 when reauth is unavailable/exhausted.
            PermissionDeniedError: On 403.
            ResourceNotFoundError: On 404.
            ClientRequestError: On any other 4xx.
            ServerError: On a 5xx that is not retryable or after retries drain.
            RetryExhaustedError: When retryable statuses or connect/timeout
                failures exhaust ``max_retries``.
        """
        client = self._get_client()

        request_timeout = timeout or self.timeout
        last_error: Exception | None = None
        did_reauth = False

        attempt = 0
        while attempt <= self.max_retries:
            # Set the session cookie on the client instance rather than passing
            # ``cookies=`` per request (httpx 0.28 deprecates per-request
            # cookies). Refreshed each iteration so a mid-loop reauth is picked
            # up on the retry.
            if self.session_token:
                client.cookies.set("JSESSIONID", self.session_token)
            else:
                client.cookies.delete("JSESSIONID")
            auth = self._get_auth()
            try:
                resp = client.request(
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

                # Handle auth errors
                if resp.status_code == 401:
                    if self.auto_reauth and not did_reauth and self.username and self.password:
                        self.authenticate()
                        did_reauth = True
                        continue

                    expired_err = SessionExpiredError(self.base_url)
                    expired_err.details.update(
                        {"status_code": resp.status_code, "method": method, "path": path}
                    )
                    raise expired_err

                if resp.status_code == 403:
                    denied_err = PermissionDeniedError(
                        resource=path,
                        operation=method.lower(),
                        url=self.base_url,
                    )
                    denied_err.details.update(
                        {"status_code": resp.status_code, "method": method, "path": path}
                    )
                    raise denied_err

                # Handle 404
                if resp.status_code == 404:
                    raise ResourceNotFoundError("resource", path)

                # Retry on retryable statuses; on exhaustion raise a typed
                # RetryExhaustedError rather than leaking httpx.HTTPStatusError.
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    last_error = ServerError(resp.status_code, method, path, _body_snippet(resp))
                    if attempt < self.max_retries:
                        retry_after = _retry_after_seconds(resp)
                        delay = (
                            retry_after
                            if retry_after is not None
                            else RETRY_BACKOFF_BASE ** (attempt + 1)
                        )
                        time.sleep(delay)
                        attempt += 1
                        continue
                    raise RetryExhaustedError("request", self.max_retries + 1, last_error)

                # Non-retryable error status: surface a typed error, never a raw
                # httpx.HTTPStatusError (401/403/404 are already handled above).
                if resp.status_code >= 400:
                    if resp.status_code < 500:
                        raise ClientRequestError(
                            resp.status_code, method, path, _body_snippet(resp)
                        )
                    raise ServerError(resp.status_code, method, path, _body_snippet(resp))

                return resp

            except httpx.ConnectError:
                last_error = ServerUnreachableError(self.base_url)
            except httpx.TimeoutException:
                last_error = NetworkError(self.base_url, f"Timeout after {request_timeout}s")
            except (AuthenticationError, ResourceNotFoundError):
                raise

            # Retry with backoff
            if attempt < self.max_retries:
                time.sleep(RETRY_BACKOFF_BASE ** (attempt + 1))

            attempt += 1

        raise RetryExhaustedError("request", self.max_retries + 1, last_error)

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

            if not results:
                break

            yield from results
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
