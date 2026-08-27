"""Input validation package for xnatctl.

Provides comprehensive validation for URLs, ports, identifiers, paths, and
DICOM-specific values. The full validation surface is re-exported here so
every existing import of a name from this package keeps working regardless
of which submodule actually defines it:

* :mod:`xnatctl.core.validation.paths` -- REST path-segment quoting and
  local filesystem path validation.
* :mod:`xnatctl.core.validation.identifiers` -- XNAT identifier/label and
  DICOM AE-title validation.
* :mod:`xnatctl.core.validation.network` -- server URL, port, timeout, and
  worker-count validation.

This module itself owns only the batch/compound validators that build on
those primitives: :func:`validate_regex_pattern`, :func:`validate_scan_ids_input`,
and :func:`validate_project_list`.
"""

from __future__ import annotations

import re

from xnatctl.core.exceptions import ConfigurationError, InvalidIdentifierError
from xnatctl.core.validation.identifiers import (
    AE_TITLE_MAX_LENGTH,
    AE_TITLE_PATTERN,
    XNAT_ID_MAX_LENGTH,
    XNAT_ID_PATTERN,
    XNAT_LABEL_FORBIDDEN_PATTERN,
    XNAT_LABEL_MAX_LENGTH,
    XNAT_RESOURCE_LABEL_FORBIDDEN_PATTERN,
    validate_ae_title,
    validate_project_id,
    validate_resource_label,
    validate_scan_id,
    validate_session_id,
    validate_subject_id,
    validate_xnat_identifier,
    validate_xnat_label,
    validate_xnat_resource_label,
)
from xnatctl.core.validation.network import (
    ALLOWED_URL_SCHEMES,
    MAX_PORT,
    MIN_PORT,
    validate_port,
    validate_server_url,
    validate_timeout,
    validate_url_or_none,
    validate_workers,
)
from xnatctl.core.validation.paths import (
    ALLOWED_ARCHIVE_EXTENSIONS,
    WINDOWS_INVALID_FILENAME_CHARS,
    WINDOWS_RESERVED_BASENAMES,
    check_no_casefold_collision,
    quote_path_segment,
    quote_prearchive_segment,
    validate_archive_path,
    validate_dicom_directory,
    validate_local_path_component,
    validate_path_exists,
    validate_path_writable,
    verify_directory_contained_in,
)

__all__ = [
    "ALLOWED_ARCHIVE_EXTENSIONS",
    "ALLOWED_URL_SCHEMES",
    "AE_TITLE_MAX_LENGTH",
    "AE_TITLE_PATTERN",
    "MAX_PORT",
    "MIN_PORT",
    "WINDOWS_INVALID_FILENAME_CHARS",
    "WINDOWS_RESERVED_BASENAMES",
    "XNAT_ID_MAX_LENGTH",
    "XNAT_ID_PATTERN",
    "XNAT_LABEL_FORBIDDEN_PATTERN",
    "XNAT_LABEL_MAX_LENGTH",
    "XNAT_RESOURCE_LABEL_FORBIDDEN_PATTERN",
    "check_no_casefold_collision",
    "quote_path_segment",
    "quote_prearchive_segment",
    "validate_ae_title",
    "validate_archive_path",
    "validate_dicom_directory",
    "validate_local_path_component",
    "validate_path_exists",
    "validate_path_writable",
    "validate_port",
    "validate_project_id",
    "validate_project_list",
    "validate_regex_pattern",
    "validate_resource_label",
    "validate_scan_id",
    "validate_scan_ids_input",
    "validate_server_url",
    "validate_session_id",
    "validate_subject_id",
    "validate_timeout",
    "validate_url_or_none",
    "validate_workers",
    "validate_xnat_identifier",
    "validate_xnat_label",
    "validate_xnat_resource_label",
    "verify_directory_contained_in",
]


# =============================================================================
# Regex Pattern Validation
# =============================================================================


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
        ) from e


# =============================================================================
# Batch Input Validation
# =============================================================================


def validate_scan_ids_input(scan_input: str) -> list[str] | None:
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
