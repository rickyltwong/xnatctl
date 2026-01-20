"""Input validation module for xnatctl.

Provides comprehensive validation for URLs, ports, identifiers, paths,
and DICOM-specific values.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from xnatctl.core.exceptions import (
    ConfigurationError,
    InvalidIdentifierError,
    InvalidPortError,
    InvalidURLError,
    PathValidationError,
)

# =============================================================================
# Constants
# =============================================================================

MIN_PORT = 1
MAX_PORT = 65535

# XNAT identifier: alphanumeric, underscore, hyphen
XNAT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
XNAT_ID_MAX_LENGTH = 64

# DICOM AE Title: 1-16 printable ASCII chars, no backslash
AE_TITLE_PATTERN = re.compile(r"^[\x20-\x5B\x5D-\x7E]{1,16}$")
AE_TITLE_MAX_LENGTH = 16

ALLOWED_URL_SCHEMES = {"http", "https"}
ALLOWED_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz"}


# =============================================================================
# URL Validation
# =============================================================================


def validate_server_url(url: str) -> str:
    """Validate XNAT server URL and return normalized form.

    Args:
        url: Server URL to validate.

    Returns:
        Normalized URL (trailing slash removed).

    Raises:
        InvalidURLError: If URL is malformed or uses unsupported scheme.
    """
    if not url or not isinstance(url, str):
        raise InvalidURLError(str(url), "URL cannot be empty")

    url = url.strip()
    if not url:
        raise InvalidURLError(url, "URL cannot be empty")

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise InvalidURLError(url, f"Failed to parse URL: {e}") from e

    if not parsed.scheme:
        raise InvalidURLError(url, "URL must include scheme (http:// or https://)")

    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise InvalidURLError(
            url,
            f"Unsupported scheme '{parsed.scheme}'. Use http or https.",
        )

    if not parsed.netloc:
        raise InvalidURLError(url, "URL must include hostname")

    return url.rstrip("/")


def validate_url_or_none(url: Optional[str]) -> Optional[str]:
    """Validate URL if provided, or return None."""
    if url is None or (isinstance(url, str) and not url.strip()):
        return None
    return validate_server_url(url)


# =============================================================================
# Port Validation
# =============================================================================


def validate_port(port: Union[int, str, None], allow_none: bool = False) -> Optional[int]:
    """Validate port number.

    Args:
        port: Port number to validate.
        allow_none: If True, None is a valid value.

    Returns:
        Validated port number or None.

    Raises:
        InvalidPortError: If port is invalid.
    """
    if port is None:
        if allow_none:
            return None
        raise InvalidPortError(port)

    try:
        port_int = int(port)
    except (ValueError, TypeError) as e:
        raise InvalidPortError(port) from e

    if port_int < MIN_PORT or port_int > MAX_PORT:
        raise InvalidPortError(port)

    return port_int


# =============================================================================
# XNAT Identifier Validation
# =============================================================================


def validate_xnat_identifier(
    value: str,
    identifier_type: str = "identifier",
    *,
    allow_empty: bool = False,
    max_length: int = XNAT_ID_MAX_LENGTH,
) -> str:
    """Validate an XNAT identifier (project, subject, session, scan ID).

    Args:
        value: Identifier value to validate.
        identifier_type: Type name for error messages.
        allow_empty: If True, empty string is valid.
        max_length: Maximum allowed length.

    Returns:
        Validated and stripped identifier.

    Raises:
        InvalidIdentifierError: If identifier is invalid.
    """
    if not isinstance(value, str):
        raise InvalidIdentifierError(identifier_type, str(value), "must be a string")

    value = value.strip()

    if not value:
        if allow_empty:
            return value
        raise InvalidIdentifierError(identifier_type, value, "cannot be empty")

    if len(value) > max_length:
        raise InvalidIdentifierError(
            identifier_type,
            value,
            f"exceeds maximum length of {max_length} characters",
        )

    if not XNAT_ID_PATTERN.match(value):
        raise InvalidIdentifierError(
            identifier_type,
            value,
            "must contain only alphanumeric characters, underscores, and hyphens",
        )

    return value


def validate_project_id(project: str) -> str:
    """Validate XNAT project ID."""
    return validate_xnat_identifier(project, "project")


def validate_subject_id(subject: str) -> str:
    """Validate XNAT subject ID."""
    return validate_xnat_identifier(subject, "subject")


def validate_session_id(session: str) -> str:
    """Validate XNAT session/experiment ID."""
    return validate_xnat_identifier(session, "session")


def validate_scan_id(scan_id: str) -> str:
    """Validate XNAT scan ID (typically numeric but XNAT allows strings)."""
    return validate_xnat_identifier(scan_id, "scan", max_length=32)


def validate_resource_label(label: str) -> str:
    """Validate XNAT resource label (more flexible than other identifiers)."""
    if not isinstance(label, str):
        raise InvalidIdentifierError("resource_label", str(label), "must be a string")

    label = label.strip()
    if not label:
        raise InvalidIdentifierError("resource_label", label, "cannot be empty")

    if "/" in label or "\\" in label:
        raise InvalidIdentifierError(
            "resource_label",
            label,
            "cannot contain path separators",
        )

    if len(label) > 64:
        raise InvalidIdentifierError(
            "resource_label",
            label,
            "exceeds maximum length of 64 characters",
        )

    return label


# =============================================================================
# DICOM Validation
# =============================================================================


def validate_ae_title(ae_title: str, field_name: str = "AE Title") -> str:
    """Validate DICOM Application Entity Title.

    Per DICOM standard: 1-16 printable ASCII characters, no backslash.
    """
    if not isinstance(ae_title, str):
        raise InvalidIdentifierError(field_name, str(ae_title), "must be a string")

    ae_title = ae_title.strip()
    if not ae_title:
        raise InvalidIdentifierError(field_name, ae_title, "cannot be empty")

    if len(ae_title) > AE_TITLE_MAX_LENGTH:
        raise InvalidIdentifierError(
            field_name,
            ae_title,
            f"exceeds maximum length of {AE_TITLE_MAX_LENGTH} characters",
        )

    if not AE_TITLE_PATTERN.match(ae_title):
        raise InvalidIdentifierError(
            field_name,
            ae_title,
            "must contain only printable ASCII characters (no backslash)",
        )

    return ae_title


# =============================================================================
# Path Validation
# =============================================================================


def validate_path_exists(
    path: Union[str, Path],
    *,
    must_be_file: bool = False,
    must_be_dir: bool = False,
    description: str = "path",
) -> Path:
    """Validate that a path exists and optionally check its type.

    Args:
        path: Path to validate.
        must_be_file: If True, path must be a file.
        must_be_dir: If True, path must be a directory.
        description: Description for error messages.

    Returns:
        Resolved Path object.

    Raises:
        PathValidationError: If path is invalid or doesn't meet requirements.
    """
    if isinstance(path, str):
        path = Path(path)

    if not path:
        raise PathValidationError(str(path), f"{description} cannot be empty")

    path = path.expanduser()

    if not path.exists():
        raise PathValidationError(str(path), f"{description} does not exist")

    if must_be_file and not path.is_file():
        raise PathValidationError(str(path), f"{description} must be a file")

    if must_be_dir and not path.is_dir():
        raise PathValidationError(str(path), f"{description} must be a directory")

    return path.resolve()


def validate_path_writable(
    path: Union[str, Path],
    description: str = "path",
) -> Path:
    """Validate that a path is writable (parent directory exists and is writable).

    Args:
        path: Path to validate.
        description: Description for error messages.

    Returns:
        Resolved Path object.

    Raises:
        PathValidationError: If path is not writable.
    """
    if isinstance(path, str):
        path = Path(path)

    path = path.expanduser()
    parent = path.parent

    if not parent.exists():
        raise PathValidationError(
            str(path),
            f"parent directory does not exist: {parent}",
        )

    if not os.access(parent, os.W_OK):
        raise PathValidationError(
            str(path),
            f"parent directory is not writable: {parent}",
        )

    return path.resolve()


def validate_archive_path(path: Union[str, Path]) -> Path:
    """Validate that path is a supported archive file.

    Args:
        path: Path to archive file.

    Returns:
        Resolved Path object.

    Raises:
        PathValidationError: If path is not a valid archive.
    """
    resolved = validate_path_exists(path, must_be_file=True, description="archive")

    suffix = resolved.suffix.lower()
    if suffix == ".gz" and resolved.stem.endswith(".tar"):
        suffix = ".tar.gz"
    elif suffix == ".tgz":
        suffix = ".tgz"

    if suffix not in ALLOWED_ARCHIVE_EXTENSIONS:
        raise PathValidationError(
            str(resolved),
            f"unsupported archive format. Allowed: {', '.join(sorted(ALLOWED_ARCHIVE_EXTENSIONS))}",
        )

    return resolved


def validate_dicom_directory(path: Union[str, Path]) -> Path:
    """Validate that path is a directory suitable for DICOM files."""
    resolved = validate_path_exists(path, must_be_dir=True, description="DICOM directory")

    if not os.access(resolved, os.R_OK):
        raise PathValidationError(str(resolved), "directory is not readable")

    return resolved


# =============================================================================
# Configuration Validation
# =============================================================================


def validate_timeout(
    value: Union[int, float, str, None],
    field_name: str = "timeout",
    *,
    min_value: int = 1,
    max_value: int = 86400 * 30,  # 30 days
    default: int = 30,
) -> int:
    """Validate timeout value in seconds.

    Args:
        value: Timeout value to validate.
        field_name: Field name for error messages.
        min_value: Minimum allowed value.
        max_value: Maximum allowed value.
        default: Default value if None.

    Returns:
        Validated timeout in seconds.

    Raises:
        ConfigurationError: If timeout is invalid.
    """
    if value is None:
        return default

    try:
        timeout = int(value)
    except (ValueError, TypeError):
        raise ConfigurationError(
            f"{field_name} must be a valid integer",
            field_name,
            value,
        )

    if timeout < min_value:
        raise ConfigurationError(
            f"{field_name} must be at least {min_value} seconds",
            field_name,
            timeout,
        )

    if timeout > max_value:
        raise ConfigurationError(
            f"{field_name} cannot exceed {max_value} seconds",
            field_name,
            timeout,
        )

    return timeout


def validate_workers(
    value: Union[int, str, None],
    field_name: str = "workers",
    *,
    min_value: int = 1,
    max_value: int = 100,
    default: int = 4,
) -> int:
    """Validate worker count for parallel operations.

    Args:
        value: Worker count to validate.
        field_name: Field name for error messages.
        min_value: Minimum allowed value.
        max_value: Maximum allowed value.
        default: Default value if None.

    Returns:
        Validated worker count.

    Raises:
        ConfigurationError: If value is invalid.
    """
    if value is None:
        return default

    try:
        workers = int(value)
    except (ValueError, TypeError):
        raise ConfigurationError(
            f"{field_name} must be a valid integer",
            field_name,
            value,
        )

    if workers < min_value:
        raise ConfigurationError(
            f"{field_name} must be at least {min_value}",
            field_name,
            workers,
        )

    if workers > max_value:
        raise ConfigurationError(
            f"{field_name} cannot exceed {max_value}",
            field_name,
            workers,
        )

    return workers


def validate_regex_pattern(pattern: str, field_name: str = "pattern") -> re.Pattern[str]:
    """Validate and compile a regex pattern.

    Args:
        pattern: Regex pattern string.
        field_name: Field name for error messages.

    Returns:
        Compiled regex pattern.

    Raises:
        ConfigurationError: If pattern is invalid.
    """
    if not pattern or not isinstance(pattern, str):
        raise ConfigurationError(f"{field_name} cannot be empty", field_name, pattern)

    try:
        return re.compile(pattern)
    except re.error as e:
        raise ConfigurationError(
            f"Invalid regex pattern: {e}",
            field_name,
            pattern,
        )


# =============================================================================
# Batch Input Validation
# =============================================================================


def validate_scan_ids_input(scan_input: str) -> Optional[list[str]]:
    """Validate and parse scan IDs input from CLI.

    Accepts:
    - "*" for all scans (returns None)
    - Comma-separated list: "1,2,3,4"
    - Single ID: "1"

    Args:
        scan_input: Raw scan IDs input string.

    Returns:
        List of scan IDs or None for all scans.

    Raises:
        InvalidIdentifierError: If any scan ID is invalid.
    """
    scan_input = scan_input.strip()

    if scan_input == "*":
        return None

    scan_ids = []
    for part in scan_input.split(","):
        part = part.strip()
        if part:
            validated = validate_scan_id(part)
            scan_ids.append(validated)

    if not scan_ids:
        raise InvalidIdentifierError("scan", scan_input, "no valid scan IDs provided")

    return scan_ids


def validate_project_list(projects_input: str) -> list[str]:
    """Validate and parse comma-separated project IDs.

    Args:
        projects_input: Comma-separated project IDs.

    Returns:
        List of validated project IDs.

    Raises:
        InvalidIdentifierError: If any project ID is invalid.
    """
    project_ids = []
    for part in projects_input.split(","):
        part = part.strip()
        if part:
            validated = validate_project_id(part)
            project_ids.append(validated)

    if not project_ids:
        raise InvalidIdentifierError("project", projects_input, "no valid project IDs provided")

    return project_ids
