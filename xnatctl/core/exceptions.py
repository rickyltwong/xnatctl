"""Exception hierarchy for xnatctl.

Provides typed exceptions for different failure modes with clear error messages.
"""

from __future__ import annotations

from typing import Any


class XNATCtlError(Exception):
    """Base exception for all xnatctl errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
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
    ):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = repr(value)
        super().__init__(message, details)
        self.field = field
        self.value = value


class ProfileNotFoundError(ConfigurationError):
    """Requested profile does not exist."""

    def __init__(self, profile: str):
        super().__init__(f"Profile not found: {profile}", field="profile", value=profile)
        self.profile = profile


# =============================================================================
# Validation Errors
# =============================================================================


class ValidationError(XNATCtlError):
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


class InvalidURLError(ValidationError):
    """Invalid URL format."""

    def __init__(self, url: str, reason: str = ""):
        msg = f"Invalid URL: {url}"
        if reason:
            msg = f"{msg} - {reason}"
        super().__init__(msg, field="url", value=url)
        self.url = url
        self.reason = reason


class InvalidPortError(ValidationError):
    """Invalid port number."""

    def __init__(self, port: Any):
        super().__init__(
            f"Invalid port: {port} (must be 1-65535)",
            field="port",
            value=port,
        )
        self.port = port


class InvalidIdentifierError(ValidationError):
    """Invalid XNAT identifier (project, subject, session, scan)."""

    def __init__(self, identifier_type: str, value: str, reason: str = ""):
        msg = f"Invalid {identifier_type}: {value}"
        if reason:
            msg = f"{msg} - {reason}"
        super().__init__(msg, field=identifier_type, value=value)
        self.identifier_type = identifier_type
        self.reason = reason


class PathValidationError(ValidationError):
    """Path validation failed."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"Invalid path: {path} - {reason}", field="path", value=path)
        self.path = path
        self.reason = reason


# =============================================================================
# Connection Errors
# =============================================================================


class ConnectionError(XNATCtlError):
    """Base class for connection-related errors."""

    def __init__(self, message: str, url: str | None = None):
        details = {"url": url} if url else {}
        super().__init__(message, details)
        self.url = url


class NetworkError(ConnectionError):
    """Network-level error (DNS, TCP, TLS)."""

    def __init__(self, url: str, cause: str | None = None):
        msg = f"Network error connecting to {url}"
        if cause:
            msg = f"{msg}: {cause}"
        super().__init__(msg, url)
        self.cause = cause


class ServerUnreachableError(ConnectionError):
    """Server is not reachable."""

    def __init__(self, url: str):
        super().__init__(f"Server unreachable: {url}", url)


class TimeoutError(ConnectionError):
    """Request timed out."""

    def __init__(self, url: str, timeout: int):
        super().__init__(f"Request timed out after {timeout}s: {url}", url)
        self.timeout = timeout


class RetryExhaustedError(ConnectionError):
    """All retry attempts failed."""

    def __init__(self, operation: str, attempts: int, last_error: Exception | None = None):
        msg = f"Operation '{operation}' failed after {attempts} attempts"
        if last_error:
            msg = f"{msg}: {last_error}"
        super().__init__(msg)
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error


# =============================================================================
# Authentication Errors
# =============================================================================


class AuthenticationError(XNATCtlError):
    """Authentication failed."""

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

    def __init__(self, url: str | None = None):
        super().__init__(url, "Session expired - please login again")


class PermissionDeniedError(AuthenticationError):
    """User lacks permission for the requested operation."""

    def __init__(self, resource: str, operation: str = "access"):
        super().__init__(reason=f"Permission denied to {operation} {resource}")
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
            f"Batch {operation} partially failed: {succeeded} succeeded, {failed} failed",
            {"succeeded": succeeded, "failed": failed},
        )
        self.succeeded = succeeded
        self.failed = failed
        self.errors = errors


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
