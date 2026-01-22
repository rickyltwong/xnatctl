"""HTTP client for XNAT REST API.

Provides retry logic, pagination, and session-based authentication.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from xnatctl.core.exceptions import (
    AuthenticationError,
    NetworkError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerUnreachableError,
)
from xnatctl.core.validation import validate_server_url

# =============================================================================
# Constants
# =============================================================================

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
RETRYABLE_STATUS_CODES = {502, 503, 504}


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
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                verify=self.verify_ssl,
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
        files: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Execute HTTP request with retry logic.

        Args:
            method: HTTP method.
            path: API path.
            params: Query parameters.
            json: JSON body.
            data: Form data or raw body.
            files: Files to upload.
            headers: Additional headers.
            timeout: Request timeout override.
            stream: Whether to stream response.

        Returns:
            HTTP response.

        Raises:
            AuthenticationError: If authentication fails.
            NetworkError: If network error occurs.
            RetryExhaustedError: If all retries fail.
        """
        client = self._get_client()
        cookies = self._get_cookies()
        auth = self._get_auth()

        request_timeout = timeout or self.timeout
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    data=data if data is not None else None,
                    files=files,
                    headers=headers,
                    cookies=cookies,
                    auth=auth,
                    timeout=request_timeout,
                )

                # Handle auth errors
                if resp.status_code in (401, 403):
                    raise AuthenticationError(
                        self.base_url,
                        "Session expired or permission denied",
                    )

                # Handle 404
                if resp.status_code == 404:
                    raise ResourceNotFoundError("resource", path)

                # Retry on server errors
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    last_error = NetworkError(
                        self.base_url,
                        f"HTTP {resp.status_code}",
                    )
                    if attempt < self.max_retries:
                        time.sleep(RETRY_BACKOFF_BASE ** (attempt + 1))
                        continue

                # Success or non-retryable error
                resp.raise_for_status()
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

        raise RetryExhaustedError("request", self.max_retries + 1, last_error)

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """GET request."""
        return self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout=timeout,
            stream=stream,
        )

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
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
        resp = self.get("/data/version")
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
        resp = self.get_json("/data/user")
        if isinstance(resp, list):
            result = resp
        elif isinstance(resp, dict):
            result = resp.get("ResultSet", {}).get("Result", [])
        elif isinstance(resp, str):
            return {
                "username": resp.strip() or "unknown",
                "firstname": "",
                "lastname": "",
                "email": "",
                "enabled": True,
            }
        else:
            result = []

        user_info = result[0] if result else {}
        if isinstance(user_info, str):
            return {
                "username": user_info.strip() or "unknown",
                "firstname": "",
                "lastname": "",
                "email": "",
                "enabled": True,
            }

        return {
            "username": user_info.get("login", "unknown"),
            "firstname": user_info.get("firstname", ""),
            "lastname": user_info.get("lastname", ""),
            "email": user_info.get("email", ""),
            "enabled": user_info.get("enabled", False),
        }
