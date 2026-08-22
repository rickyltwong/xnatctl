"""Input validation module for xnatctl.

Provides comprehensive validation for URLs, ports, identifiers, paths,
and DICOM-specific values.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse

from xnatctl.core.exceptions import (
    ConfigurationError,
    InvalidIdentifierError,
    InvalidPortError,
    InvalidURLError,
    PathValidationError,
)
from xnatctl.core.redact import redact_url_userinfo
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS

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

# Windows reserved device basenames -- matched case-insensitively, and
# against the stem only (the part before the first '.'), since Windows
# treats "NUL.txt" and "nul.tar.gz" as the same reserved device NUL is.
# COM¹/COM²/COM³ and LPT¹/LPT²/LPT³ (the
# superscript-digit forms of COM1-3/LPT1-3, U+00B9/U+00B2/U+00B3) are
# reserved by Windows the same way -- included alongside the plain-digit
# forms rather than relying on NFC normalization to catch them, since these
# are distinct precomposed characters, not a decomposition of the ASCII
# digit.
WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    | {"COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
)

# Characters Windows filesystem APIs reject in a filename outright (':' is
# checked separately above, with its own message about drive/ADS syntax).
# Rejected on every platform, not just when actually running on Windows:
# a value legal on XNAT but unwritable on Windows should fail the same way
# regardless of which machine runs the download, not only there.
WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>"|?*')


# =============================================================================
# Path Segment Quoting
# =============================================================================


def _reject_empty_or_dot_only_segment(value: str) -> None:
    """Shared guard for both path-segment quoting helpers below.

    Raises:
        InvalidIdentifierError: If ``value`` is empty, whitespace-only, or
            composed entirely of dots (``.``, ``..``, ...).

            Empty matters because a raw call site that skips ref
            construction (``quote_path_segment(project_id)`` directly, say)
            would otherwise build a COLLECTION route instead of an item
            route -- ``ProjectService.delete("")`` silently turning into
            ``DELETE /data/projects/`` rather than failing on a bad ID.

            Dot-only matters because ``quote()`` treats ``.`` as unreserved,
            so leaving dots literal (required for real labels containing
            them) would otherwise let a bare ``..`` segment through
            unescaped -- the one shape that still means something to an
            HTTP path resolver even after this function's other
            reserved-character encoding. Mirrors the same rule in
            :func:`validate_xnat_label`.
    """
    if value.strip() == "":
        raise InvalidIdentifierError("path segment", value, "cannot be empty or whitespace-only")
    if value.strip(".") == "":
        raise InvalidIdentifierError("path segment", value, "cannot be composed entirely of dots")


def quote_path_segment(value: str) -> str:
    """Percent-encode a single ``/data/...`` REST path segment.

    Dots are left literal: XNAT subject and experiment labels routinely
    contain them (e.g. imported from DICOM PatientName), and the ``/data/``
    hierarchy endpoints expect them literal. Use :func:`quote_prearchive_segment`
    instead for prearchive/archive-move endpoints, where a literal dot is
    parsed by XNAT's own path grammar and must be escaped.

    Raises:
        InvalidIdentifierError: See :func:`_reject_empty_or_dot_only_segment`.
    """
    _reject_empty_or_dot_only_segment(value)
    return quote(value, safe="")


def quote_prearchive_segment(value: str) -> str:
    """Percent-encode a single prearchive/archive-move REST path segment.

    XNAT's prearchive and archive "move" services (``/data/prearchive/...``,
    the ``src``/``dest`` values posted to ``/data/services/archive``) parse a
    literal ``.`` in a segment as part of their own path grammar, so it is
    additionally escaped as ``%2E`` on top of normal percent-encoding. This is
    XNAT-specific and load-bearing for those routes -- do not use it for
    ordinary ``/data/...`` hierarchy paths (see :func:`quote_path_segment`).

    Raises:
        InvalidIdentifierError: If ``value`` is empty or whitespace-only.
            (A dot-only value is not rejected here the way it is in
            :func:`quote_path_segment` -- it doesn't need to be: this
            function %2E-encodes every dot, so ``".."`` already comes out as
            ``"%2E%2E"``, never surviving as a literal ``".."``.)
    """
    if value.strip() == "":
        raise InvalidIdentifierError("path segment", value, "cannot be empty or whitespace-only")
    return quote(value, safe="").replace(".", "%2E")


def validate_local_path_component(value: str, label: str = "value") -> str:
    """Validate that *value* is safe to use as ONE local filesystem path component.

    Used where a server-reported identifier (a scan ID, a resource label)
    is joined onto a local directory to build a download destination.
    Unlike :func:`quote_path_segment` -- which neutralizes a hostile URL
    segment by percent-encoding it -- there is no equivalent "make it safe"
    transform for a local join: reducing a value to its final path
    component (``Path(value).name``) would silently alias distinct inputs
    onto the same local destination (two different hostile scan IDs both
    landing on a generic fallback name, then overwriting each other), and a
    value that is itself an absolute path or ``".."`` can yield an EMPTY
    ``.name``, which a naive ``.name or fallback`` still has to fall back to
    *something* -- exactly the kind of substitution that becomes a bug the
    moment the fallback is chosen carelessly.

    So this accepts only values that are ALREADY their own safe form and
    rejects everything else outright: a hostile or merely-malformed
    server-reported identifier fails the download/extraction it belongs to,
    rather than being silently reduced to a fallback name that could collide
    with another scan/resource's legitimate destination. "Safe form" is
    judged against Windows and macOS filesystem quirks too, not just POSIX,
    since this package runs (and is CI-tested) on Windows:

    * A drive-qualified or drive-relative value (``"C:"``, ``"C:escape"``,
      or the NTFS alternate-data-stream form ``"file:stream"``) contains no
      ``/`` or ``\\`` at all, so the separator check alone misses it -- but
      ``Path(base) / "C:escape"`` on Windows DISCARDS ``base`` entirely
      (a drive-relative path replaces whatever it's joined onto, the same
      way an absolute path does), escaping containment before the result is
      even resolved. Any ``:`` is rejected outright.
    * A trailing dot or trailing space is silently stripped by the Windows
      filesystem APIs, so ``"scan."`` and ``"scan"`` -- or ``"scan "`` and
      ``"scan"`` -- land on the same real file there, even though they are
      different strings here. Leading dots/spaces are rejected too, for the
      same "looks different, lands the same" aliasing reason, and to keep
      the rule symmetric rather than punishing only one side; a dot or space
      INSIDE the value is unaffected (``"John.Doe"``, ``"QA (v2)"`` still
      pass -- see :func:`validate_xnat_label`'s tests for the label-level
      equivalents).
    * ``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``-``COM9``, and
      ``LPT1``-``LPT9`` are reserved device names on Windows, matched
      case-insensitively against the stem regardless of extension --
      ``"NUL"``, ``"nul.txt"``, and ``"Nul.tar.gz"`` all fail to create as a
      normal file/directory there.
    * A value not already in Unicode NFC form (e.g. an NFD-decomposed
      accented character) can be byte-distinct from its NFC form while
      denoting the same filename on a normalizing filesystem (macOS/HFS+ in
      particular) -- two values that look different here would alias to one
      real path there. XNAT's own server-reported values are ASCII in
      practice, so this cannot over-reject anything real; it exists purely
      to keep this function injective under both Windows/macOS aliasing.

    This is deliberately stricter than what is legal in a URL path segment:
    :func:`validate_xnat_resource_label` allows ``#``, ``?``, and ``%`` in a
    resource label, since those are routine on XNAT and the URL-quoting
    layer encodes them unambiguously -- but ``?`` (and the other Windows
    reserved characters above) still fail here. A resource labelled
    ``"QA?1"`` is a legal XNAT label and fetchable over HTTP, but a download
    naming a local file or directory after it will raise instead of
    guessing a substitution. That asymmetry -- URL-legal does not imply
    locally-writable -- is intentional: this layer's job is to fail loudly
    on every platform for a value that cannot be written verbatim, not to
    silently reinterpret it into something that can.

    Args:
        value: The identifier to validate (used verbatim as a filesystem
            path component if it passes).
        label: Field name for the error message.

    Returns:
        ``value``, unchanged.

    Raises:
        PathValidationError: If ``value`` is empty or whitespace-only,
            contains a C0 control character (``\\x00``-``\\x1f``, including
            NUL), a path separator (``/`` or ``\\``), ``:``, or another
            character reserved in Windows filenames (``< > " | ? *``), is
            composed entirely of dots (``.``, ``..``, ...), starts or ends
            with a dot or space, is a Windows-reserved device basename
            (case-insensitive, any extension), or is not already in Unicode
            NFC form.
    """
    if value.strip() == "":
        raise PathValidationError(value, f"{label} cannot be empty or whitespace-only")
    if any(ch < "\x20" for ch in value):
        raise PathValidationError(
            value, f"{label} cannot contain a control character (including NUL)"
        )
    if "/" in value or "\\" in value:
        raise PathValidationError(value, f"{label} cannot contain a path separator")
    if ":" in value:
        raise PathValidationError(
            value, f"{label} cannot contain ':' (Windows drive/alternate-data-stream syntax)"
        )
    if WINDOWS_INVALID_FILENAME_CHARS.intersection(value):
        raise PathValidationError(
            value,
            f"{label} cannot contain '<', '>', '\"', '|', '?', or '*' "
            "(invalid in a Windows filename) -- rejected on every platform "
            "so a value that is legal on XNAT does not fail only on some "
            "download machines",
        )
    if value.strip(".") == "":
        raise PathValidationError(value, f"{label} cannot be composed entirely of dots")
    if value[0] in ". " or value[-1] in ". ":
        raise PathValidationError(
            value, f"{label} cannot start or end with a dot or space (aliases on Windows)"
        )
    stem = value.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_BASENAMES:
        raise PathValidationError(value, f"{label} is a reserved Windows device name")
    if value != unicodedata.normalize("NFC", value):
        raise PathValidationError(value, f"{label} must be Unicode NFC-normalized")
    return value


def check_no_casefold_collision(
    value: str, seen_casefolded: set[str], label: str = "value"
) -> None:
    """Raise if *value* collides case-insensitively with an already-seen sibling.

    ``validate_local_path_component`` deliberately does not reject case by
    itself -- case is not a property of one value, only of how it compares
    to values that will sit BESIDE it as siblings under the same directory.
    "scan" and "SCAN" are each individually a perfectly fine local path
    component, but a case-insensitive filesystem (Windows, and macOS/HFS+ by
    default) treats them as the SAME file -- so when a batch of sibling
    components is created together (scan directories in a session download,
    session-resource ZIP names), a value colliding with an earlier one in
    the same batch must fail rather than silently overwrite it.

    Call once per sibling, in creation order, threading the same
    ``seen_casefolded`` set through the whole batch (a plain ``set()`` the
    caller owns -- this function only reads and mutates it, so it is safe to
    call repeatedly across a sequential loop; callers that parallelize
    across siblings must synchronize their own access to the set).

    Args:
        value: The path component about to be created.
        seen_casefolded: Mutable set of casefolded values already registered
            in this batch; ``value``'s casefolded form is added to it.
        label: Field name for the error message.

    Raises:
        PathValidationError: If ``value``'s casefolded form is already in
            ``seen_casefolded``.
    """
    folded = value.casefold()
    if folded in seen_casefolded:
        raise PathValidationError(
            value,
            f"{label} collides case-insensitively with another value already used in "
            "this batch (a case-insensitive filesystem -- Windows, or macOS/HFS+ by "
            "default -- would treat them as the same file)",
        )
    seen_casefolded.add(folded)


def verify_directory_contained_in(candidate: Path, trusted_root: Path, label: str = "path") -> Path:
    """Verify *candidate* resolves to somewhere inside *trusted_root*.

    ``validate_local_path_component`` makes an identifier safe to APPEND as
    one path component, but that is not the whole story: a caller-trusted
    base directory (``output_dir``, ``session_dir`` -- something the CALLER
    chose, e.g. via ``--out``) joined with that identifier is not
    automatically safe just because the identifier itself passed. If the
    resulting subdirectory ALREADY EXISTS on disk as a symlink pointing
    outside ``trusted_root`` -- planted by an earlier run, a race, or
    deliberately -- resolving it walks straight through the symlink to
    wherever it points. Every containment check performed AFTER that
    resolution (the zip-slip guards in
    :mod:`xnatctl.services.zip_extract`, for instance) ends up anchored to
    the escaped location, and passes trivially for every member that is
    "inside" it -- because the check was comparing the escaped directory
    against itself.

    Call this on the identifier-joined directory itself, before doing
    anything with it (creating it, extracting into it), with
    ``trusted_root`` the caller-supplied ancestor the join must never
    escape -- one level up from the identifier-derived subdirectory, not
    the subdirectory's own (already-resolved) path.

    Args:
        candidate: The identifier-joined directory to verify (e.g.
            ``output_dir / resource_label``).
        trusted_root: The caller-supplied ancestor directory ``candidate``
            must resolve inside of (e.g. ``output_dir``).
        label: Field name for the error message.

    Returns:
        ``candidate.resolve()``.

    Raises:
        PathValidationError: If ``candidate``'s resolved form is not
            ``trusted_root``'s resolved form or a descendant of it.
    """
    resolved_root = trusted_root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise PathValidationError(
            str(candidate),
            f"{label} resolves outside its trusted root {resolved_root} "
            "(possibly a pre-existing symlink) -- refusing to use it",
        )
    return resolved_candidate


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

    # Every raise below reports the redacted form: InvalidURLError echoes the
    # value into its message *and* keeps it as `.value`, so a rejected
    # `https://admin:s3cret@host` would otherwise leak the password through the
    # error path it was rejected by.
    safe = redact_url_userinfo(url)

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise InvalidURLError(safe, f"Failed to parse URL: {redact_url_userinfo(str(e))}") from e

    if not parsed.scheme:
        raise InvalidURLError(safe, "URL must include scheme (http:// or https://)")

    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise InvalidURLError(
            safe,
            f"Unsupported scheme '{parsed.scheme}'. Use http or https.",
        )

    if not parsed.netloc:
        raise InvalidURLError(safe, "URL must include hostname")

    # Reject embedded credentials rather than stripping them. Stripping would
    # silently drop credentials the user believed were in effect, and the URL
    # is copied into `base_url`, which surfaces in error messages, log lines
    # and `config show`.
    if parsed.username or parsed.password:
        raise InvalidURLError(
            safe,
            "Do not embed credentials in the URL. Use a profile, "
            "XNAT_USER/XNAT_PASS, or `xnatctl auth login`.",
        )

    return url.rstrip("/")


def validate_url_or_none(url: str | None) -> str | None:
    """Validate URL if provided, or return None."""
    if url is None or (isinstance(url, str) and not url.strip()):
        return None
    return validate_server_url(url)


# =============================================================================
# Port Validation
# =============================================================================


def validate_port(port: int | str | None, allow_none: bool = False) -> int | None:
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
    path: str | Path,
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
    path: str | Path,
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


def validate_archive_path(path: str | Path) -> Path:
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


def validate_dicom_directory(path: str | Path) -> Path:
    """Validate that path is a directory suitable for DICOM files."""
    resolved = validate_path_exists(path, must_be_dir=True, description="DICOM directory")

    if not os.access(resolved, os.R_OK):
        raise PathValidationError(str(resolved), "directory is not readable")

    return resolved


# =============================================================================
# Configuration Validation
# =============================================================================


def validate_timeout(
    value: int | float | str | None,
    field_name: str = "timeout",
    *,
    min_value: int = 1,
    max_value: int = 86400 * 30,  # 30 days
    default: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
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
    except (ValueError, TypeError) as e:
        raise ConfigurationError(
            f"{field_name} must be a valid integer",
            field_name,
            value,
        ) from e

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
    value: int | str | None,
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
    except (ValueError, TypeError) as e:
        raise ConfigurationError(
            f"{field_name} must be a valid integer",
            field_name,
            value,
        ) from e

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
