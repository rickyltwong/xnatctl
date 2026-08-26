"""Local filesystem and REST path-segment validation.

Covers two related but distinct concerns: percent-encoding a value for use
as one REST path segment (:func:`quote_path_segment`,
:func:`quote_prearchive_segment`), and validating that a server-reported
identifier is safe to use as a local filesystem path component or that a
caller-supplied path exists, is writable, or resolves inside a trusted root
(:func:`validate_local_path_component`, :func:`check_no_casefold_collision`,
:func:`verify_directory_contained_in`, :func:`validate_path_exists`,
:func:`validate_path_writable`, :func:`validate_archive_path`,
:func:`validate_dicom_directory`).
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from urllib.parse import quote

from xnatctl.core.exceptions import InvalidIdentifierError, PathValidationError

# =============================================================================
# Constants
# =============================================================================

ALLOWED_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz"}

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
