"""Exception hierarchy for xnatctl.

Provides typed exceptions for different failure modes with clear error messages.
"""

from __future__ import annotations

import warnings
from typing import Any


class XNATCtlError(Exception):
    """Base exception for all xnatctl errors.

    ``str(exc)`` is the human-facing message and nothing else. ``details``
    is kept for verbose output and structured rendering, never appended to
    the message: doing so would turn "Profile not found: prod" into
    "Profile not found: prod (field=profile, value='prod')", where the suffix
    only restates the message as debug noise.
    """

    #: Next step for this class of error, shown under the message as
    #: ``Try: ...``. Set it on a subclass when there is a genuinely clear
    #: action; leaving it None is correct when there is not, because a vague
    #: hint is worse than none. Instances may override via ``hint=``.
    default_hint: str | None = None

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.hint = hint if hint is not None else self.default_hint

    def __str__(self) -> str:
        return self.message


# =============================================================================
# Configuration Errors
# =============================================================================


class ConfigurationError(XNATCtlError):
    """Error in configuration (missing, invalid, or malformed)."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
        value: Any = None,
        hint: str | None = None,
    ):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = repr(value)
        super().__init__(message, details, hint=hint)
        self.field = field
        self.value = value


class NoConfigurationError(ConfigurationError):
    """No profiles are configured at all -- the first-run state.

    Distinct from :class:`ProfileNotFoundError`, which means "that particular
    profile is missing". On a fresh machine the honest problem is that nothing
    has been configured yet, and telling the user their *default* profile was
    not found sends them looking for a typo that does not exist.
    """

    default_hint = "Run 'xnatctl config init' to create one."


class ProfileNotFoundError(ConfigurationError):
    """Requested profile does not exist."""

    default_hint = (
        "Run 'xnatctl config show' to list profiles, or 'xnatctl config init' to create one."
    )

    def __init__(self, profile: str):
        super().__init__(f"Profile not found: {profile}", field="profile", value=profile)
        self.profile = profile


# =============================================================================
# Validation Errors
# =============================================================================


class InputValidationError(XNATCtlError):
    """Input validation failed."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
        value: Any = None,
    ):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = repr(value)
        super().__init__(message, details)
        self.field = field
        self.value = value


class InvalidURLError(InputValidationError):
    """Invalid URL format."""

    def __init__(self, url: str, reason: str = ""):
        msg = f"Invalid URL: {url}"
        if reason:
            msg = f"{msg} - {reason}"
        super().__init__(msg, field="url", value=url)
        self.url = url
        self.reason = reason


class InvalidPortError(InputValidationError):
    """Invalid port number."""

    def __init__(self, port: Any):
        super().__init__(
            f"Invalid port: {port} (must be 1-65535)",
            field="port",
            value=port,
        )
        self.port = port


class InvalidIdentifierError(InputValidationError):
    """Invalid XNAT identifier (project, subject, session, scan)."""

    def __init__(self, identifier_type: str, value: str, reason: str = ""):
        msg = f"Invalid {identifier_type}: {value}"
        if reason:
            msg = f"{msg} - {reason}"
        super().__init__(msg, field=identifier_type, value=value)
        self.identifier_type = identifier_type
        self.reason = reason


class PathValidationError(InputValidationError):
    """Path validation failed."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"Invalid path: {path} - {reason}", field="path", value=path)
        self.path = path
        self.reason = reason


class ValidationError(InputValidationError):
    """Deprecated alias for :class:`InputValidationError`.

    Renamed to stop colliding with ``pydantic.ValidationError``, which this
    package also surfaces through model validation. Never raised internally;
    the classes raised by the library are :class:`InputValidationError`
    instances, so ``except ValidationError`` no longer matches them.
    Instantiating this alias warns; it is removed in a later minor release.
    """

    def __init__(
        self,
        message: str,
        field: str | None = None,
        value: Any = None,
    ):
        warnings.warn(
            "ValidationError is deprecated; use InputValidationError instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(message, field=field, value=value)


# =============================================================================
# Connection Errors
# =============================================================================


class XNATConnectionError(XNATCtlError):
    """Base class for connection-related errors."""

    def __init__(self, message: str, url: str | None = None):
        details = {"url": url} if url else {}
        super().__init__(message, details)
        self.url = url


class NetworkError(XNATConnectionError):
    """Network-level error (DNS, TCP, TLS)."""

    def __init__(self, url: str, cause: str | None = None):
        msg = f"Network error connecting to {url}"
        if cause:
            msg = f"{msg}: {cause}"
        super().__init__(msg, url)
        self.cause = cause


class ServerUnreachableError(XNATConnectionError):
    """Server is not reachable."""

    default_hint = "Check the server URL, and whether you need a VPN to reach it."

    def __init__(self, url: str):
        super().__init__(f"Server unreachable: {url}", url)


class RequestTimeoutError(XNATConnectionError):
    """Request timed out.

    Raised by ``XNATClient`` when the CONNECT phase times out (host blackholed /
    firewall-DROPped), so ``timeout`` is the connect timeout in seconds. This
    fails fast and is not retried.

    Also raised for a READ-phase timeout on a non-idempotent method:
    the server has already seen the request, so retrying could execute it twice.
    Those carry an explicit ``message`` saying the operation may have partially
    executed. Read-phase timeouts on idempotent methods are retried and remain
    the generic ``NetworkError`` bucket.
    """

    def __init__(self, url: str, timeout: int, message: str | None = None):
        super().__init__(message or f"Could not connect to {url} within {timeout}s", url)
        self.timeout = timeout


class RetryExhaustedError(XNATConnectionError):
    """All retry attempts failed."""

    def __init__(self, operation: str, attempts: int, last_error: Exception | None = None):
        msg = f"Operation '{operation}' failed after {attempts} attempts"
        if last_error:
            msg = f"{msg}: {last_error}"
        super().__init__(msg)
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error


class ConnectionError(XNATConnectionError):
    """Deprecated alias for :class:`XNATConnectionError`.

    Renamed off the builtin ``ConnectionError`` name it shadowed. Never raised
    internally; the connection errors the library raises (``NetworkError``,
    ``RequestTimeoutError``, ...) are :class:`XNATConnectionError` instances, so
    ``except ConnectionError`` no longer matches them. Instantiating this alias
    warns; it is removed in a later minor release.
    """

    def __init__(self, message: str, url: str | None = None):
        warnings.warn(
            "ConnectionError is deprecated; use XNATConnectionError instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(message, url)


class TimeoutError(RequestTimeoutError):
    """Deprecated alias for :class:`RequestTimeoutError`.

    Renamed off the builtin ``TimeoutError`` name it shadowed. Never raised
    internally; ``XNATClient`` raises :class:`RequestTimeoutError`, so
    ``except TimeoutError`` no longer matches it. Instantiating this alias
    warns; it is removed in a later minor release.
    """

    def __init__(self, url: str, timeout: int, message: str | None = None):
        warnings.warn(
            "TimeoutError is deprecated; use RequestTimeoutError instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(url, timeout, message)


# =============================================================================
# HTTP Status Errors
# =============================================================================


class HTTPResponseError(XNATCtlError):
    """Base for an HTTP error status with no more specific typed handler.

    Carries ``status_code``, ``method``, ``path``, and a redacted body snippet
    in ``details`` so callers and ``handle_errors`` never see a raw
    ``httpx.HTTPStatusError``.
    """

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        body: str = "",
    ):
        msg = f"HTTP {status_code} on {method} {path}"
        details: dict[str, Any] = {
            "status_code": status_code,
            "method": method,
            "path": path,
        }
        if body:
            details["body"] = body
        super().__init__(msg, details)
        self.status_code = status_code
        self.method = method
        self.path = path
        self.body = body


class ClientRequestError(HTTPResponseError):
    """A 4xx response not covered by auth/permission/not-found (e.g. 400, 409, 422)."""


class ServerError(HTTPResponseError):
    """A 5xx response (500, 502, 503, 504)."""


# =============================================================================
# Authentication Errors
# =============================================================================


class AuthenticationError(XNATCtlError):
    """Authentication failed."""

    default_hint = "Run 'xnatctl auth login', or set XNAT_USER and XNAT_PASS."

    def __init__(self, url: str | None = None, reason: str = ""):
        msg = "Authentication failed"
        if url:
            msg = f"{msg} for {url}"
        if reason:
            msg = f"{msg}: {reason}"
        details = {"url": url} if url else {}
        super().__init__(msg, details)
        self.url = url
        self.reason = reason


class SessionExpiredError(AuthenticationError):
    """Session has expired."""

    default_hint = "Your session expired. Run 'xnatctl auth login' again."

    def __init__(self, url: str | None = None):
        super().__init__(url, "Session expired - please login again")


class PermissionDeniedError(AuthenticationError):
    """User lacks permission for the requested operation."""

    default_hint = "Check that your XNAT account has the required role on this project."

    def __init__(self, resource: str, operation: str = "access", url: str | None = None):
        super().__init__(url, reason=f"Permission denied to {operation} {resource}")
        self.resource = resource
        self.operation = operation


# =============================================================================
# Resource Errors
# =============================================================================


class ResourceError(XNATCtlError):
    """Error related to XNAT resources."""

    def __init__(
        self,
        message: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ):
        details = {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_id:
            details["resource_id"] = resource_id
        super().__init__(message, details)
        self.resource_type = resource_type
        self.resource_id = resource_id


class ResourceNotFoundError(ResourceError):
    """Requested resource does not exist."""

    default_hint = (
        "Check the ID or label. Labels require -P/--project (or default_project in the profile)."
    )

    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            f"{resource_type} not found: {resource_id}",
            resource_type,
            resource_id,
        )


class ResourceExistsError(ResourceError):
    """Resource already exists."""

    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            f"{resource_type} already exists: {resource_id}",
            resource_type,
            resource_id,
        )


# =============================================================================
# Operation Errors
# =============================================================================


class OperationError(XNATCtlError):
    """Error during an operation."""

    def __init__(
        self,
        operation: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        full_details = {"operation": operation}
        if details:
            full_details.update(details)
        super().__init__(message, full_details)
        self.operation = operation


class UploadError(OperationError):
    """Error during upload."""

    def __init__(
        self,
        message: str,
        file_path: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        full_details = details or {}
        if file_path:
            full_details["file"] = file_path
        super().__init__("upload", message, full_details)
        self.file_path = file_path


class DownloadError(OperationError):
    """Error during download."""

    def __init__(
        self,
        message: str,
        resource: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        full_details = details or {}
        if resource:
            full_details["resource"] = resource
        super().__init__("download", message, full_details)
        self.resource = resource


class UpgradeError(OperationError):
    """Error self-updating a frozen (PyInstaller) binary install.

    Only raised by ``cli/upgrade.py``'s frozen-binary path (download, sha256
    verification, and the atomic swap of ``sys.executable``); the
    package-manager paths (pipx/pip/uv/docker) print a command instead of
    running anything themselves, so they have nothing of this shape to fail.
    """

    default_hint = "See the release notes: https://github.com/rickyltwong/xnatctl/releases"

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__("upgrade", message, details)


class BatchOperationError(OperationError):
    """Error in batch operation with partial success."""

    def __init__(
        self,
        operation: str,
        succeeded: int,
        failed: int,
        errors: list[str],
    ):
        super().__init__(
            operation,
            f"Batch {operation} did not fully succeed: {succeeded} succeeded, {failed} failed",
            {"succeeded": succeeded, "failed": failed},
        )
        self.succeeded = succeeded
        self.failed = failed
        self.errors = errors


# =============================================================================
# Compatibility Errors
# =============================================================================


class UnsupportedServerVersionError(XNATCtlError):
    """A feature requires a newer XNAT server than the one reporting in.

    Raised only when the server's version is known and below the floor --
    see ``core.server_version.require_server_version``. An unknown version
    fails open rather than raising this.
    """

    def __init__(
        self,
        feature_name: str,
        minimum: tuple[int, int, int],
        actual: tuple[int, int, int],
    ):
        min_str = ".".join(str(part) for part in minimum)
        actual_str = ".".join(str(part) for part in actual)
        msg = f"{feature_name} requires XNAT >= {min_str}; server reports {actual_str}"
        super().__init__(
            msg,
            {"feature": feature_name, "minimum": min_str, "actual": actual_str},
        )
        self.feature_name = feature_name
        self.minimum = minimum
        self.actual = actual


# =============================================================================
# DICOM Errors
# =============================================================================


class DicomError(XNATCtlError):
    """Error related to DICOM operations."""

    def __init__(self, message: str, file_path: str | None = None):
        details = {"file": file_path} if file_path else {}
        super().__init__(message, details)
        self.file_path = file_path


class DicomParseError(DicomError):
    """Failed to parse DICOM file."""

    def __init__(self, file_path: str, reason: str = ""):
        msg = f"Failed to parse DICOM file: {file_path}"
        if reason:
            msg = f"{msg} - {reason}"
        super().__init__(msg, file_path)
        self.reason = reason


class DicomStoreError(DicomError):
    """DICOM C-STORE operation failed."""

    def __init__(self, message: str, host: str | None = None, port: int | None = None):
        super().__init__(message)
        self.host = host
        self.port = port
        if host:
            self.details["host"] = host
        if port:
            self.details["port"] = port


# =============================================================================
# Transfer Errors
# =============================================================================


class TransferError(OperationError):
    """Error during project transfer."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("transfer", message, details)


class TransferConflictError(TransferError):
    """Conflict detected on destination during transfer."""

    def __init__(
        self,
        entity_type: str,
        local_id: str,
        remote_id: str,
        reason: str,
    ):
        super().__init__(
            f"Conflict on {entity_type} {local_id} (remote {remote_id}): {reason}",
            {"entity_type": entity_type, "local_id": local_id, "remote_id": remote_id},
        )
        self.entity_type = entity_type
        self.local_id = local_id
        self.remote_id = remote_id
        self.reason = reason


class TransferCircuitBreakerError(TransferError):
    """Too many consecutive transfer failures."""

    def __init__(self, failures: int, max_failures: int):
        super().__init__(
            f"Circuit breaker: {failures}/{max_failures} consecutive failures",
            {"failures": failures, "max_failures": max_failures},
        )
        self.failures = failures
        self.max_failures = max_failures


class TransferVerificationError(TransferError):
    """Post-transfer verification failed."""

    def __init__(self, entity_id: str, expected: int, actual: int):
        super().__init__(
            f"Verification failed for {entity_id}: expected {expected} files, got {actual}",
            {"entity_id": entity_id, "expected": expected, "actual": actual},
        )
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual


class TransferConfigError(TransferError):
    """Invalid transfer configuration."""

    def __init__(self, message: str, field: str | None = None):
        details: dict[str, Any] = {}
        if field:
            details["field"] = field
        super().__init__(message, details)
        self.field = field


class OperationCancelledError(XNATCtlError):
    """The user interrupted the operation.

    Not a failure: nothing went wrong, the run was stopped on request. Kept
    distinct so a cancelled batch is never reported as data the server
    rejected, and so it maps to the user-cancelled exit code rather than a
    general error.
    """

    def __init__(self, operation: str = "operation"):
        super().__init__(f"Cancelled: {operation}", {"operation": operation})
        self.operation = operation
