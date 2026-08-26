"""XNAT identifier, label, and DICOM AE-title validation.

Covers strict admin-chosen identifiers (:func:`validate_xnat_identifier` and
its project/subject/session/scan wrappers), the looser labels that come from
imported DICOM metadata (:func:`validate_xnat_label`,
:func:`validate_xnat_resource_label`, :func:`validate_resource_label`), and
the DICOM Application Entity Title format (:func:`validate_ae_title`).
"""

from __future__ import annotations

import re

from xnatctl.core.exceptions import InvalidIdentifierError

# =============================================================================
# Constants
# =============================================================================

# XNAT identifier: alphanumeric, underscore, hyphen
XNAT_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]+")
XNAT_ID_MAX_LENGTH = 64

# DICOM AE Title: 1-16 printable ASCII chars, no backslash
AE_TITLE_PATTERN = re.compile(r"[\x20-\x5B\x5D-\x7E]{1,16}")
AE_TITLE_MAX_LENGTH = 16

# XNAT labels (subject/experiment labels, resource labels) are looser than
# XNAT_ID_PATTERN in the wild -- dots, spaces, and parentheses show up
# routinely from imported DICOM metadata. Only characters that could redirect
# a REST request to a different route or resource are forbidden: path
# separators, the URL-reserved '?'/'#', '%' (which would smuggle a second
# layer of percent-encoding past the quoting helpers below), and control
# characters -- C0 (\x00-\x1f), DEL, and the C1 range (\x80-\x9f), which are
# just as invisible/unprintable as C0 but sit past the ASCII range so a
# purely-ASCII pattern would miss them.
XNAT_LABEL_FORBIDDEN_PATTERN = re.compile(r"[/\\?#%\x00-\x1f\x7f-\x9f]")
XNAT_LABEL_MAX_LENGTH = 255


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

    if not XNAT_ID_PATTERN.fullmatch(value):
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


# Resource labels are a step looser again than XNAT_LABEL_FORBIDDEN_PATTERN:
# '#', '?', and '%' show up in real resource labels (XNAT does not restrict
# them at creation time, and resources are typically named once and never
# renamed), and the quoting layer (quote_path_segment) percent-encodes all
# three without ambiguity -- so there is no request-routing reason to reject
# them here the way there is for '/' or a bare '..'.
XNAT_RESOURCE_LABEL_FORBIDDEN_PATTERN = re.compile(r"[/\\\x00-\x1f\x7f-\x9f]")


def _validate_looser_xnat_label(
    value: str,
    label_type: str,
    *,
    allow_empty: bool,
    max_length: int,
    forbidden_pattern: re.Pattern[str],
    forbidden_description: str,
) -> str:
    """Shared body for :func:`validate_xnat_label` and :func:`validate_xnat_resource_label`.

    Both accept dots, spaces, parentheses, and unicode -- routine once a
    label comes from imported metadata rather than an admin-chosen ID --
    while still rejecting whatever could redirect a REST request to a
    different route or resource. They differ only in whether ``#``/``?``/``%``
    are part of that "could redirect" set; see ``forbidden_pattern``.
    """
    if not isinstance(value, str):
        raise InvalidIdentifierError(label_type, str(value), "must be a string")

    if not value:
        if allow_empty:
            return value
        raise InvalidIdentifierError(label_type, value, "cannot be empty")

    if value != value.strip():
        raise InvalidIdentifierError(
            label_type, value, "cannot have leading or trailing whitespace"
        )

    if len(value) > max_length:
        raise InvalidIdentifierError(
            label_type,
            value,
            f"exceeds maximum length of {max_length} characters",
        )

    if forbidden_pattern.search(value):
        raise InvalidIdentifierError(label_type, value, f"cannot contain {forbidden_description}")

    # A segment made up of only dots (".", "..", "...") is never a real
    # label -- dots are otherwise allowed literal because labels commonly
    # contain them, but a dot-only segment is exactly the shape a path
    # traversal needs, and /data/ paths deliberately do not %2E-encode dots
    # the way the prearchive endpoints do (see quote_path_segment).
    if value.strip(".") == "":
        raise InvalidIdentifierError(
            label_type,
            value,
            "cannot be composed entirely of dots",
        )

    return value


def validate_xnat_label(
    value: str,
    label_type: str = "label",
    *,
    allow_empty: bool = False,
    max_length: int = XNAT_LABEL_MAX_LENGTH,
) -> str:
    """Validate a looser XNAT label (project/subject/experiment/scan identity).

    Real XNAT labels are looser than :func:`validate_xnat_identifier` allows
    -- spaces, dots, and parentheses show up routinely once labels come from
    imported DICOM metadata rather than admin-chosen IDs. This still rejects
    everything that could redirect a REST request to a different route or
    resource: path separators, ``?``/``#``, ``%``, control characters,
    leading/trailing whitespace, and a dot-only segment (``.``/``..``).

    Args:
        value: Label value to validate.
        label_type: Type name for error messages.
        allow_empty: If True, empty string is valid.
        max_length: Maximum allowed length.

    Returns:
        The validated label, unchanged.

    Raises:
        InvalidIdentifierError: If the label is invalid.
    """
    return _validate_looser_xnat_label(
        value,
        label_type,
        allow_empty=allow_empty,
        max_length=max_length,
        forbidden_pattern=XNAT_LABEL_FORBIDDEN_PATTERN,
        forbidden_description="'/', '\\', '?', '#', '%', or control characters",
    )


def validate_xnat_resource_label(
    value: str,
    label_type: str = "resource_label",
    *,
    allow_empty: bool = False,
    max_length: int = XNAT_LABEL_MAX_LENGTH,
) -> str:
    """Validate a resource label -- looser again than :func:`validate_xnat_label`.

    Resource labels routinely carry ``#``, ``?``, or ``%`` in the wild (XNAT
    never restricted them at creation time), and :func:`quote_path_segment`
    percent-encodes all three unambiguously, so there is no routing reason to
    reject them here. Still rejects path separators, control characters,
    leading/trailing whitespace, empty, and a dot-only segment (``.``/``..``)
    -- the same "could redirect the request" set minus the three characters
    the quoting layer already neutralizes without help.

    Args:
        value: Resource label to validate.
        label_type: Type name for error messages.
        allow_empty: If True, empty string is valid.
        max_length: Maximum allowed length.

    Returns:
        The validated label, unchanged.

    Raises:
        InvalidIdentifierError: If the label is invalid.
    """
    return _validate_looser_xnat_label(
        value,
        label_type,
        allow_empty=allow_empty,
        max_length=max_length,
        forbidden_pattern=XNAT_RESOURCE_LABEL_FORBIDDEN_PATTERN,
        forbidden_description="'/', '\\', or control characters",
    )


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

    if not AE_TITLE_PATTERN.fullmatch(ae_title):
        raise InvalidIdentifierError(
            field_name,
            ae_title,
            "must contain only printable ASCII characters (no backslash)",
        )

    return ae_title
