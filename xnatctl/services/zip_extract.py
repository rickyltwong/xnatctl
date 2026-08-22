"""ZIP extraction into the standard XNAT on-disk layout.

Pure filesystem logic, split out of
:mod:`xnatctl.services.downloads` -- these functions only unpack an
already-downloaded archive; they make no HTTP calls of their own.
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

from xnatctl.core.exceptions import DownloadError, PathValidationError
from xnatctl.core.validation import validate_local_path_component


def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> None:
    """Extract ZIP contents safely, guarding against path traversal."""
    resolved_root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (extract_dir / member.filename).resolve()
            if not target.is_relative_to(resolved_root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_scan_zip(  # noqa: C901  # pre-existing; see pyproject
    zip_path: Path,
    scan_base: Path,
    *,
    resource_label: str | None = None,
    exclude_resources: frozenset[str] = frozenset(),
) -> tuple[int, int]:
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
        Tuple of (files_extracted, duplicates_renamed).

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
    # Anchored once, from the caller-trusted argument -- never recomputed
    # from a path that a resource label has already been joined onto. The
    # label is validated below, but this containment check must not depend
    # on that validation catching everything: if ``target_dir`` were built
    # from an unsafe label FIRST and *then* resolved into the "root" the
    # check compares against, the check would trivially pass (it would be
    # comparing the escaped directory against itself).
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

    return files_extracted, duplicates_renamed


def extract_session_zips(
    session_dir: Path,
    *,
    cleanup: bool = True,
    on_message: Callable[[str], None] | None = None,
    zip_paths: Sequence[Path] | None = None,
) -> None:
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

    Raises:
        DownloadError: If any archive is corrupt; extraction of a truncated
            download must fail the command rather than report a partial success.
    """
    zip_files = list(zip_paths) if zip_paths is not None else list(session_dir.glob("*.zip"))
    if not zip_files:
        return

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
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue

                    member_path = Path(member)
                    if any(part.startswith(".") for part in member_path.parts):
                        continue

                    parts = member_path.parts
                    if len(parts) > 1:
                        stripped_path = Path(*parts[1:])
                    else:
                        stripped_path = member_path

                    target_path = session_dir / stripped_path
                    # Guard against ZipSlip path traversal
                    if not target_path.resolve().is_relative_to(session_dir.resolve()):
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)

            if cleanup:
                zip_path.unlink()
                if on_message is not None:
                    on_message(f"  Removed {zip_path.name}")
        except zipfile.BadZipFile:
            failed_zips.append(zip_path.name)

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
