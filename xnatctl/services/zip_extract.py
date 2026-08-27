"""ZIP extraction into the standard XNAT on-disk layout.

Pure filesystem logic, split out of
:mod:`xnatctl.services.downloads` -- these functions only unpack an
already-downloaded archive; they make no HTTP calls of their own.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

from xnatctl.core.exceptions import DownloadError, PathValidationError
from xnatctl.core.validation import validate_local_path_component

logger = logging.getLogger(__name__)

# The Unix file-type nibble lives in the top 16 bits of ZipInfo.external_attr
# (the low 16 bits are the DOS attribute byte, unused here). A ZIP written on
# a non-Unix system, or with no Unix attributes set, reads as 0 here -- never
# a false positive for "symlink".
_EXTERNAL_ATTR_TYPE_MASK = 0o170000
_EXTERNAL_ATTR_SYMLINK = 0o120000


def _is_symlink_member(member: zipfile.ZipInfo) -> bool:
    """True if a ZIP member's Unix mode bits mark it as a symlink.

    ``zf.open``/``shutil.copyfileobj`` always writes a member's bytes as a
    regular file -- a symlink-typed member is never materialized as an actual
    OS-level symlink, so it cannot itself be used to escape the extraction
    root. But its content is the symlink's *target path*, not real file
    content, which is still surprising output for a caller who asked for
    DICOM/NIfTI/etc. data -- worth flagging even though it is safe to write.
    """
    return (member.external_attr >> 16) & _EXTERNAL_ATTR_TYPE_MASK == _EXTERNAL_ATTR_SYMLINK


def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> list[str]:
    """Extract ZIP contents safely, guarding against path traversal.

    Returns:
        Filenames of members skipped as unsafe -- those that failed the
        containment check, and symlink-typed members (treated the same way:
        not written, so a caller can log and count them rather than the skip
        happening silently).
    """
    # Created before it is resolved -- see the matching note on
    # `resolved_scan_base` in `_extract_scan_zip` below. Creating the
    # trusted root itself (never a label- or member-derived path) is safe;
    # it just makes `resolve()` observe the same "exists" filesystem state
    # every time it is called against this root.
    extract_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = extract_dir.resolve()
    skipped: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (extract_dir / member.filename).resolve()
            if not target.is_relative_to(resolved_root):
                skipped.append(member.filename)
                continue
            if _is_symlink_member(member):
                skipped.append(member.filename)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return skipped


def _extract_scan_zip(  # noqa: C901  # pre-existing; see pyproject
    zip_path: Path,
    scan_base: Path,
    *,
    resource_label: str | None = None,
    exclude_resources: frozenset[str] = frozenset(),
) -> tuple[int, int, list[str]]:
    """Extract a scan ZIP into standard XNAT layout.

    Handles both filtered ZIPs (single resource) and unfiltered ZIPs
    (multiple resources).  XNAT ZIP structure:
        {exp}/scans/{id}/resources/{label}/files/{filename...}

    Args:
        zip_path: Path to the downloaded ZIP file.
        scan_base: Target directory (e.g. session_dir/scans/{scan_id}).
        resource_label: If set, all files go under resources/{label}/files/.
            When None, resource labels are inferred from ZIP paths.
        exclude_resources: Resource labels to skip during extraction.

    Returns:
        Tuple of (files_extracted, duplicates_renamed, skipped_names).
        ``skipped_names`` holds the filenames of members skipped as unsafe --
        containment-check failures and symlink-typed members -- so nothing
        is dropped without a trace.

    Raises:
        PathValidationError: If ``resource_label`` is an explicit empty or
            whitespace-only string (``None`` still means "no override" and
            falls back to a per-member detected label); or if a resource
            label that will actually be extracted (whether the
            caller-supplied ``resource_label`` or one detected from a ZIP
            member's own internal path) is not safe to use as a local
            directory name -- see
            :func:`~xnatctl.core.validation.validate_local_path_component`
            -- or if two DIFFERENT literal labels within this ZIP collide
            case-insensitively (see ``label_casefold_registry`` below). A
            label belonging only to members in ``exclude_resources`` skips
            both checks, since it is never written. A hostile or colliding
            label fails the whole extraction rather than being silently
            reduced to (or merged into) a destination that could alias onto
            another resource's legitimate files.
    """
    files_extracted = 0
    duplicates_renamed = 0
    skipped: list[str] = []
    # Anchored once, from the caller-trusted argument -- never recomputed
    # from a path that a resource label has already been joined onto. The
    # label is validated below, but this containment check must not depend
    # on that validation catching everything: if ``target_dir`` were built
    # from an unsafe label FIRST and *then* resolved into the "root" the
    # check compares against, the check would trivially pass (it would be
    # comparing the escaped directory against itself).
    #
    # Created before it is resolved. On Windows, `Path.resolve()` can only
    # canonicalize (correct case, expand any 8.3 short name, follow a
    # substituted drive/junction) the deepest ALREADY-EXISTING ancestor of
    # the path; everything below that is appended as literal, unresolved
    # text (see `ntpath._getfinalpathname_nonstrict`). Multiple scans
    # extract concurrently into sibling directories under the same shared
    # parent (`session_dir/scans`) -- if that parent didn't exist yet when
    # THIS call resolved `scan_base`, but a DIFFERENT worker's `mkdir`
    # created it before THIS call resolved a member's `dest` a few lines
    # down, the two `.resolve()` calls can canonicalize different amounts
    # of the same path and disagree at the byte level even though they name
    # the same directory -- failing `is_relative_to` on a perfectly
    # contained file. Ensuring the root exists first makes both `.resolve()`
    # calls observe the same "already exists" filesystem state, so they
    # canonicalize identically. This mkdir is still safe: `scan_base` is the
    # caller-trusted root (validated scan_id, already checked one level up
    # against `session_dir`), never a path built from a member's own
    # (attacker-influenced) resource label -- the escape this function
    # guards against below.
    scan_base.mkdir(parents=True, exist_ok=True)
    resolved_scan_base = scan_base.resolve()

    # `is not None`, not truthy: an explicitly-supplied empty resource_label
    # is a caller mistake, not "no override" -- silently falling through to
    # the per-member detected label (or "UNKNOWN") would extract into a
    # destination the caller never asked for. Checked once, not per member,
    # since resource_label does not vary across the ZIP.
    if resource_label is not None and resource_label.strip() == "":
        raise PathValidationError(
            resource_label, "resource_label cannot be empty or whitespace-only"
        )

    # Maps casefolded label -> the first literal label seen for it. A ZIP
    # legitimately has MANY members sharing one resource label (every file
    # under "DICOM/", say), so this cannot be a simple "seen once" set --
    # the same literal string recurring is fine. What is NOT fine is a
    # second, DIFFERENT literal label that folds to the same key ("DICOM"
    # then "dicom"): both are individually valid local directory names, but
    # a case-insensitive filesystem (Windows, macOS/HFS+ by default) would
    # merge them into the same directory, silently interleaving two
    # resources' files.
    label_casefold_registry: dict[str, str] = {}

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            member_path = Path(member.filename)
            if any(part.startswith(".") for part in member_path.parts):
                continue

            parts = member_path.parts

            # Detect resource label and relative file path from ZIP entry.
            detected_label: str | None = None
            rel: Path | None = None
            if "resources" in parts and "files" in parts:
                res_idx = parts.index("resources")
                files_idx = parts.index("files")
                if files_idx > res_idx + 1:
                    detected_label = parts[res_idx + 1]
                    rel_parts = parts[files_idx + 1 :]
                    if rel_parts:
                        rel = Path(*rel_parts)

            # Fallback: strip up to "files/" if present, else strip top folder.
            if rel is None:
                if "files" in parts:
                    idx = parts.index("files")
                    rel_parts = parts[idx + 1 :]
                    if not rel_parts:
                        continue
                    rel = Path(*rel_parts)
                elif len(parts) > 1:
                    rel = Path(*parts[1:])
                else:
                    rel = member_path

            if not rel.name or rel.name.startswith("."):
                continue

            effective_label = resource_label or detected_label or "UNKNOWN"

            # Exclusion is checked BEFORE validation/registration: an
            # explicitly-excluded resource is never extracted, so a locally
            # unsafe label on it (or a case collision against another
            # excluded label) must not fail the whole ZIP for content that
            # is never written. Only labels that will actually be extracted
            # go through the safety check below.
            if effective_label in exclude_resources:
                continue

            effective_label = validate_local_path_component(effective_label, "resource label")

            folded_label = effective_label.casefold()
            existing_literal = label_casefold_registry.get(folded_label)
            if existing_literal is None:
                label_casefold_registry[folded_label] = effective_label
            elif existing_literal != effective_label:
                raise PathValidationError(
                    effective_label,
                    f"resource label collides case-insensitively with '{existing_literal}' "
                    "already used earlier in this ZIP (a case-insensitive filesystem -- "
                    "Windows, or macOS/HFS+ by default -- would merge them into one "
                    "directory, interleaving two different resources' files)",
                )

            target_dir = scan_base / "resources" / effective_label / "files"

            dest = (target_dir / rel).resolve()
            if not dest.is_relative_to(resolved_scan_base):
                skipped.append(member.filename)
                continue
            if _is_symlink_member(member):
                skipped.append(member.filename)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)

            final_dest = dest
            if final_dest.exists():
                duplicates_renamed += 1
                stem = final_dest.stem
                suffix = final_dest.suffix
                i = 1
                while True:
                    candidate = final_dest.with_name(f"{stem}__dup{i}{suffix}")
                    if not candidate.exists():
                        final_dest = candidate
                        break
                    i += 1

            with zf.open(member) as src, open(final_dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files_extracted += 1

    return files_extracted, duplicates_renamed, skipped


def _extract_session_zip_member(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, session_dir: Path
) -> str | None:
    """Extract one member of a session ZIP.

    Strips the member's top-level (session label) path component.

    Returns:
        The member's filename if it was skipped as unsafe (a containment-
        check failure or a symlink-typed member); ``None`` if it was a
        directory entry, a hidden file (both intentionally filtered, not
        "unsafe"), or was extracted normally.
    """
    member = info.filename
    if member.endswith("/"):
        return None

    member_path = Path(member)
    if any(part.startswith(".") for part in member_path.parts):
        return None

    parts = member_path.parts
    stripped_path = Path(*parts[1:]) if len(parts) > 1 else member_path

    target_path = session_dir / stripped_path
    # Guard against ZipSlip path traversal
    if not target_path.resolve().is_relative_to(session_dir.resolve()):
        return member
    if _is_symlink_member(info):
        return member

    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as source, open(target_path, "wb") as target:
        shutil.copyfileobj(source, target)
    return None


def extract_session_zips(
    session_dir: Path,
    *,
    cleanup: bool = True,
    on_message: Callable[[str], None] | None = None,
    zip_paths: Sequence[Path] | None = None,
    skipped_out: list[str] | None = None,
) -> list[str]:
    """Extract ZIP files in a session directory.

    Args:
        session_dir: Path to session directory containing ZIPs.
        cleanup: Remove ZIPs after successful extraction.
        on_message: Called with each user-facing progress line (rendering is
            the caller's concern; the service prints nothing).
        zip_paths: Exactly which ZIPs to extract; None globs every ``*.zip``
            in *session_dir* (the default). A caller that wants to defer some
            ZIPs -- e.g. session-resource ZIPs that ``--verify`` still needs
            to read intact -- passes the rest explicitly instead.
        skipped_out: When given, used as the skipped-name accumulator itself,
            so entries recorded before a corrupt archive raises are not lost
            to the caller (the return value alone cannot carry them past the
            exception).

    Returns:
        Filenames of members skipped as unsafe across every ZIP processed in
        this call -- containment-check failures and symlink-typed members.
        Also logged as one warning per call when nonempty.

    Raises:
        DownloadError: If any archive is corrupt; extraction of a truncated
            download must fail the command rather than report a partial success.
    """
    # `_extract_session_zip_member` resolves `session_dir` fresh on every
    # member (see the note there); this call runs single-threaded today, but
    # ensuring the root exists up front -- same reasoning as
    # `_extract_scan_zip`'s `resolved_scan_base` -- keeps that guarantee
    # true regardless of caller concurrency, rather than relying on every
    # caller happening to have created it first.
    session_dir.mkdir(parents=True, exist_ok=True)
    zip_files = list(zip_paths) if zip_paths is not None else list(session_dir.glob("*.zip"))
    skipped = skipped_out if skipped_out is not None else []
    if not zip_files:
        return skipped

    failed_zips: list[str] = []
    for zip_path in zip_files:
        if on_message is not None:
            on_message(f"Extracting {zip_path.name}...")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # CRC-check every member before writing anything: testzip()
                # returns the first corrupt name, or None when the archive is
                # whole. Without it a truncated download extracted partially and
                # the command still reported success.
                if zf.testzip() is not None:
                    failed_zips.append(zip_path.name)
                    continue
                # infolist(), not namelist(): the symlink check needs each
                # member's external_attr, which only ZipInfo carries.
                for info in zf.infolist():
                    skipped_name = _extract_session_zip_member(zf, info, session_dir)
                    if skipped_name is not None:
                        skipped.append(skipped_name)

            if cleanup:
                zip_path.unlink()
                if on_message is not None:
                    on_message(f"  Removed {zip_path.name}")
        except zipfile.BadZipFile:
            failed_zips.append(zip_path.name)

    if skipped:
        logger.warning(
            "Skipped %d unsafe ZIP entries during extraction: %s",
            len(skipped),
            skipped[:5],
        )

    if failed_zips:
        # A corrupt archive must fail the command, not print and exit 0:
        # @handle_errors turns this into a nonzero exit so a broken download is
        # never mistaken for a complete one.
        raise DownloadError(
            "Corrupt ZIP archive(s) in the session download: "
            + ", ".join(failed_zips)
            + ". Extraction did not complete.",
            details={"corrupt_zips": failed_zips},
        )

    return skipped
