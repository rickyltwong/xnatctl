"""Single resource/scan/multi-scan ZIP download paths.

Each method here fetches one archive over ``/data/experiments/{id}/...`` and
optionally extracts it -- as distinct from :mod:`.session`'s per-scan
parallel engine, these are single-request downloads.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from xnatctl.core.exceptions import (
    AuthenticationError,
    DownloadError,
    InputValidationError,
    ResourceNotFoundError,
    XNATCtlError,
)
from xnatctl.core.validation import (
    quote_path_segment,
    validate_local_path_component,
    validate_xnat_resource_label,
    verify_directory_contained_in,
)
from xnatctl.models.hierarchy import ExperimentRef, ResourceRef, ScanRef
from xnatctl.models.progress import DownloadProgress, DownloadSummary, OperationPhase

from ..hierarchy import HierarchyService
from ..zip_extract import _safe_extract_zip
from .shared import _HierarchyResolveMixin, _safe_output_path
from .transport import stream_to_file

logger = logging.getLogger(__name__)


class _ResourceScanDownloadMixin(_HierarchyResolveMixin):
    """Mixin providing single-resource and single/multi-scan download methods."""

    def download_resource(
        self,
        session_id: str,
        resource_label: str,
        output_dir: Path,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = False,
        zip_filename: str | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            output_dir: Output directory
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP files (default: False)
            zip_filename: Custom ZIP filename (default: {resource_label}.zip)
            progress_callback: Progress callback

        Returns:
            DownloadSummary describing the completed download (always a success;
            failures raise).

        Raises:
            XNATCtlError: A typed failure from the client layer passes through
                untouched -- authentication, permission, not-found, or a
                short-read DownloadError. The one carve-out: when ``session_id``
                is a label needing resolution to an experiment ID, a non-404,
                non-auth typed failure during that resolution step (a network
                hiccup, a 5xx) is swallowed into a best-effort fallback that
                treats ``session_id`` as the experiment ID directly, rather
                than raised here.
            DownloadError: Any other failure (OSError, corrupt ZIP, unexpected
                exception) wrapped with the resource label and ``__cause__`` set.
        """
        start_time = time.time()
        output_dir = Path(output_dir)

        def notify_error(exc: Exception) -> None:
            # The notification must never mask the failure it reports, so a
            # raising callback is suppressed.
            if progress_callback is None:
                return
            with contextlib.suppress(Exception):
                progress_callback(
                    DownloadProgress(
                        phase=OperationPhase.ERROR,
                        message=str(exc),
                        success=False,
                        errors=[str(exc)],
                    )
                )

        try:
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                resolved_experiment_ref = self._resolve_zip_experiment_ref(
                    session_id,
                    project=project,
                )
            except AuthenticationError:
                # Covers SessionExpiredError and PermissionDeniedError too --
                # an auth failure here will just fail again on the fallback
                # path, so surfacing it directly is more honest than masking
                # it with a doomed retry.
                raise
            except (ResourceNotFoundError, ValueError):
                # A definitive 404 or a malformed response means the
                # identifier itself is bad, not that resolution merely
                # hiccuped -- it must not be swallowed by the fallback below.
                raise
            except XNATCtlError:
                # Any other typed failure (network, server, retry-exhausted)
                # is deliberately discarded: resolution is best-effort
                # normalization, and a transient hiccup here must not doom an
                # otherwise-valid accession ID. The fallback retries via the
                # direct /data/experiments/{id} path -- if that also fails,
                # ITS error is the one that propagates under this method's
                # contract.
                resolved_experiment_ref = ExperimentRef(experiment=session_id)

            # Build path - always use /data/experiments/{id}/... for reliable ZIP downloads.
            # `is not None`, not truthy: `scan_id=""` is a caller mistake, not
            # "no scan scope" -- it must not silently widen to a session-level
            # resource. ScanRef's own validation rejects the empty string.
            if scan_id is not None:
                path = HierarchyService.build_resource_path(
                    ResourceRef(
                        parent=ScanRef(experiment=resolved_experiment_ref, scan_id=scan_id),
                        resource_label=resource_label,
                    ),
                    "files",
                )
            else:
                path = HierarchyService.build_resource_path(
                    ResourceRef(parent=resolved_experiment_ref, resource_label=resource_label),
                    "files",
                )

            params = {"format": "zip"}

            # `resource_label` reaches Windows path handling raw wherever it
            # is joined onto a local path below (e.g. "C:escape" is
            # drive-relative, discarding the base entirely) unless validated
            # first -- validate_xnat_resource_label above only covers URL
            # safety, not local-filesystem safety.
            safe_resource_label = validate_local_path_component(resource_label, "resource_label")
            zip_path = _safe_output_path(output_dir, zip_filename, f"{safe_resource_label}.zip")

            progress_cb: Callable[[int, int | None], None] | None = None
            if progress_callback is not None:
                emit = progress_callback

                def progress_cb(written: int, total: int | None) -> None:
                    emit(
                        DownloadProgress(
                            phase=OperationPhase.DOWNLOADING,
                            bytes_received=written,
                            total_bytes=total or 0,
                            file_path=str(zip_path),
                        )
                    )

            total_bytes = stream_to_file(
                self.client, path, zip_path, params=params, progress_cb=progress_cb
            ).bytes_written

            file_count = 1
            skipped: list[str] = []
            if extract:
                extract_dir = output_dir / safe_resource_label
                # A pre-existing symlink at exactly this path would resolve
                # OUTSIDE output_dir -- _safe_extract_zip's own containment
                # check then anchors to that escaped location. Verified one
                # level up, against the true caller-supplied root.
                verify_directory_contained_in(extract_dir, output_dir, "extraction directory")
                skipped = _safe_extract_zip(zip_path, extract_dir)
                if skipped:
                    logger.warning(
                        "Skipped %d unsafe ZIP entries during extraction: %s",
                        len(skipped),
                        skipped[:5],
                    )
                file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
                zip_path.unlink()

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=1,
                succeeded=1,
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=str(output_dir),
                session_id=session_id,
                skipped_unsafe_entries=len(skipped),
            )

        except XNATCtlError as e:
            # Typed failures already carry the right class and exit code -- an
            # expired session, a permission denial, a 404, or the DownloadError
            # stream_to_file raises on a short read. Passing them through is the
            # whole point: a caller can distinguish them instead of reading a
            # stringified summary.
            notify_error(e)
            raise
        except Exception as e:
            notify_error(e)
            raise DownloadError(str(e), resource=resource_label) from e

    def download_scan(
        self,
        session_id: str,
        scan_id: str,
        output_dir: Path,
        project: str | None = None,
        resource: str | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            output_dir: Output directory
            project: Project ID
            resource: Resource type to download (None = all resources)
            progress_callback: Progress callback

        Returns:
            DownloadSummary describing the download. With ``resource=None`` this
            is the multi-scan batch summary from :meth:`download_scans` (call its
            ``raise_for_status`` to fail on a partial result); with a resource it
            is the always-success summary from :meth:`download_resource`.

        Raises:
            XNATCtlError: A typed client-layer failure from the single-resource
                path (:meth:`download_resource`) passes through untouched.
            DownloadError: Any other single-resource failure, wrapped.
        """
        if resource is None:
            return self.download_scans(
                session_id=session_id,
                scan_ids=[scan_id],
                output_dir=output_dir,
                project=project,
                resource=None,
                progress_callback=progress_callback,
            )
        return self.download_resource(
            session_id=session_id,
            resource_label=resource,
            output_dir=output_dir,
            scan_id=scan_id,
            project=project,
            progress_callback=progress_callback,
        )

    def download_scans(
        self,
        session_id: str,
        scan_ids: list[str],
        output_dir: Path,
        project: str | None = None,
        subject: str | None = None,
        resource: str | None = None,
        zip_filename: str | None = None,
        extract: bool = False,
        cleanup: bool = True,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download multiple scans in a single request.

        Uses XNAT's comma-separated scan ID feature for efficient batch downloads.
        When resource is None, downloads ALL files (DICOM + SNAPSHOTS).

        Args:
            session_id: Session ID or label
            scan_ids: List of scan IDs (or ["ALL"] for all scans)
            output_dir: Output directory
            project: Project ID (required when using session label)
            subject: Subject ID/label (optional, narrows experiment lookup)
            resource: Resource type (None = all resources, "DICOM" = DICOM only)
            zip_filename: Output ZIP filename (default: scans.zip)
            extract: Extract ZIP after download
            cleanup: Remove ZIP after successful extraction (with extract=True)
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results. This is a batch operation: a failed
            fetch is reported as ``success=False`` with the reason in ``errors``
            rather than raised. Call ``raise_for_status()`` on the summary to
            turn a failed batch into a ``BatchOperationError``.

        Raises:
            InputValidationError: If ``scan_ids`` is empty, or contains an
                empty/whitespace-only ID. An empty list would otherwise join
                to an empty batch spec (``/scans//files`` -- a malformed,
                different route), and this is caught before any HTTP call or
                filesystem write, not folded into the batch-failure summary
                the way a stream failure is.
        """
        if not scan_ids:
            raise InputValidationError("scan_ids cannot be empty", field="scan_ids", value=scan_ids)
        if any(not scan_id.strip() for scan_id in scan_ids):
            raise InputValidationError(
                "scan_ids cannot contain an empty or whitespace-only ID",
                field="scan_ids",
                value=scan_ids,
            )

        start_time = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            resolved_experiment_ref = self._resolve_zip_experiment_ref(
                session_id,
                project=project,
                subject=subject,
            )
        except AuthenticationError:
            raise
        except (ResourceNotFoundError, ValueError):
            # A definitive 404 or a malformed response means the identifier
            # itself is bad; do not paper over it with the fallback below.
            raise
        except XNATCtlError:
            # Best-effort normalization -- see the sibling try/except in
            # download_resource for the full rationale.
            resolved_experiment_ref = ExperimentRef(experiment=session_id)

        # XNAT's own comma-delimited multi-scan syntax (`/scans/1,2,3/files`) --
        # NOT a single opaque path segment, so it cannot go through ScanRef +
        # the normal builder chain the way one scan ID does: that would quote
        # the whole joined string as one segment, percent-encoding the very
        # commas that make it a batch spec (and double-encoding any ID that
        # itself needed escaping). Each ID is quoted on its own instead, then
        # rejoined with a literal comma.
        scans_suffix = ",".join(quote_path_segment(scan_id) for scan_id in scan_ids)
        scans_base_path = HierarchyService.build_experiment_path(
            HierarchyService.routable_scan_parent(resolved_experiment_ref), "scans"
        )

        # `is not None`, not truthy: `resource=""` is a caller mistake, not
        # "no resource filter" -- it must not silently widen to all
        # resources. validate_xnat_resource_label rejects it explicitly.
        if resource is not None:
            resource = validate_xnat_resource_label(resource)
            path = (
                f"{scans_base_path}/{scans_suffix}/resources/{quote_path_segment(resource)}/files"
            )
        else:
            path = f"{scans_base_path}/{scans_suffix}/files"

        params = {"format": "zip"}
        zip_path = _safe_output_path(output_dir, zip_filename, "scans.zip")

        try:
            progress_cb: Callable[[int, int | None], None] | None = None
            if progress_callback is not None:
                emit = progress_callback

                def progress_cb(written: int, total: int | None) -> None:
                    emit(
                        DownloadProgress(
                            phase=OperationPhase.DOWNLOADING,
                            bytes_received=written,
                            total_bytes=total or 0,
                            file_path=str(zip_path),
                        )
                    )

            total_bytes = stream_to_file(
                self.client, path, zip_path, params=params, progress_cb=progress_cb
            ).bytes_written

            file_count = 1
            output_path = str(zip_path)
            skipped: list[str] = []
            if extract:
                extract_dir = output_dir / "scans"
                # "scans" is a fixed literal, not identifier-derived, but the
                # same symlink risk applies to any pre-existing entry at this
                # path -- verified against the true caller-supplied root.
                verify_directory_contained_in(extract_dir, output_dir, "extraction directory")
                skipped = _safe_extract_zip(zip_path, extract_dir)
                if skipped:
                    logger.warning(
                        "Skipped %d unsafe ZIP entries during extraction: %s",
                        len(skipped),
                        skipped[:5],
                    )
                file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
                if cleanup:
                    zip_path.unlink()
                output_path = str(extract_dir)

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=len(scan_ids),
                succeeded=len(scan_ids),
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=output_path,
                session_id=session_id,
                skipped_unsafe_entries=len(skipped),
            )

        except Exception as e:  # noqa: BLE001 -- documented batch contract: failure reported via DownloadSummary, not raised
            duration = time.time() - start_time
            return DownloadSummary(
                success=False,
                total=len(scan_ids),
                succeeded=0,
                failed=len(scan_ids),
                duration=duration,
                errors=[str(e)],
                session_id=session_id,
            )
