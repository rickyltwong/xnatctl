"""Upload service package for XNAT upload operations.

Provides ``UploadService`` with methods for all upload transports:
- REST batch upload (simple ZIP batches via import service) -- :mod:`.rest_batch`
- Parallel REST upload (batched archives with parallel workers) -- :mod:`.rest_batch`
- Gradual-DICOM upload (parallel per-file REST import) -- :mod:`.gradual`
- DICOM C-STORE upload (pynetdicom-based network transfer) -- :mod:`.dicom_store`
- Resource upload (file/directory upload to session resources) -- :mod:`.resources`

Public utility functions (``collect_dicom_files``, ``split_into_batches``,
``upload_archive_or_raise``, etc.) are re-exported here for direct import and
testing; each transport module also exposes them directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from xnatctl.models.progress import UploadProgress, UploadSummary

from ..base import BaseService
from . import dicom_store, gradual, resources, rest_batch
from .dicom_store import (
    DEFAULT_DICOM_CALLING_AET,
    DEFAULT_DICOM_PORT,
    DEFAULT_DICOM_STORE_WORKERS,
    DICOMStoreSummary,
    build_dicom_tls_context,
)
from .gradual import GradualUploadRun
from .gradual_client import GradualClientPool
from .rest_batch import (
    DEFAULT_ARCHIVE_FORMAT,
    DEFAULT_ARCHIVE_WORKERS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_IMPORT_HANDLER,
    DEFAULT_OVERWRITE,
    DEFAULT_TIMEOUT,
    ArchiveUploadResult,
    upload_archive_or_raise,
    upload_single_archive,
)
from .shared import (
    DEFAULT_UPLOAD_WORKERS,
    DICOM_EXTENSIONS,
    SessionRefresher,
    collect_dicom_files,
    split_into_batches,
    split_into_n_batches,
)

__all__ = [
    "DEFAULT_ARCHIVE_FORMAT",
    "DEFAULT_ARCHIVE_WORKERS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DICOM_CALLING_AET",
    "DEFAULT_DICOM_PORT",
    "DEFAULT_DICOM_STORE_WORKERS",
    "DEFAULT_IMPORT_HANDLER",
    "DEFAULT_OVERWRITE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_UPLOAD_WORKERS",
    "DICOM_EXTENSIONS",
    "ArchiveUploadResult",
    "DICOMStoreSummary",
    "GradualClientPool",
    "GradualUploadRun",
    "SessionRefresher",
    "UploadService",
    "build_dicom_tls_context",
    "collect_dicom_files",
    "split_into_batches",
    "split_into_n_batches",
    "upload_archive_or_raise",
    "upload_single_archive",
]


class UploadService(BaseService):
    """Service for XNAT upload operations.

    Provides methods for all upload transports: REST batch, parallel REST,
    gradual-DICOM, DICOM C-STORE, and resource uploads. Each method is a thin
    wrapper that binds ``self.client`` and delegates to the corresponding
    transport module -- the mechanics live there, not here.
    """

    def upload_dicom_parallel(
        self,
        source_dir: Path,
        project: str,
        subject: str,
        session: str,
        *,
        username: str | None = None,
        password: str | None = None,
        upload_workers: int = DEFAULT_UPLOAD_WORKERS,
        archive_workers: int = rest_batch.DEFAULT_ARCHIVE_WORKERS,
        archive_format: str = rest_batch.DEFAULT_ARCHIVE_FORMAT,
        import_handler: str = rest_batch.DEFAULT_IMPORT_HANDLER,
        ignore_unparsable: bool = True,
        overwrite: str = rest_batch.DEFAULT_OVERWRITE,
        direct_archive: bool = True,
        timeout: int = rest_batch.DEFAULT_TIMEOUT,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload DICOM files using parallel batched archives via REST import.

        See :func:`xnatctl.services.upload.rest_batch.upload_dicom_parallel`.
        """
        return rest_batch.upload_dicom_parallel(
            self.client,
            source_dir,
            project,
            subject,
            session,
            username=username,
            password=password,
            upload_workers=upload_workers,
            archive_workers=archive_workers,
            archive_format=archive_format,
            import_handler=import_handler,
            ignore_unparsable=ignore_unparsable,
            overwrite=overwrite,
            direct_archive=direct_archive,
            timeout=timeout,
            progress_callback=progress_callback,
        )

    def upload_dicom_store(
        self,
        dicom_root: Path,
        host: str,
        called_aet: str,
        *,
        port: int = dicom_store.DEFAULT_DICOM_PORT,
        calling_aet: str = dicom_store.DEFAULT_DICOM_CALLING_AET,
        workers: int = dicom_store.DEFAULT_DICOM_STORE_WORKERS,
        cleanup: bool = True,
        tls: bool = False,
        tls_ca_bundle: str | None = None,
        tls_cert: str | None = None,
        tls_key: str | None = None,
    ) -> DICOMStoreSummary:
        """Send DICOM files to an SCP using C-STORE.

        See :func:`xnatctl.services.upload.dicom_store.upload_dicom_store`.
        Independent of ``self.client``: C-STORE talks directly to the SCP.
        """
        return dicom_store.upload_dicom_store(
            dicom_root,
            host,
            called_aet,
            port=port,
            calling_aet=calling_aet,
            workers=workers,
            cleanup=cleanup,
            tls=tls,
            tls_ca_bundle=tls_ca_bundle,
            tls_cert=tls_cert,
            tls_key=tls_key,
        )

    def upload_dicom_gradual(
        self,
        source_path: Path,
        project: str,
        subject: str,
        session: str,
        *,
        workers: int = DEFAULT_UPLOAD_WORKERS,
        direct_archive: bool = True,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload DICOM files using the gradual-DICOM handler (parallel per-file).

        See :func:`xnatctl.services.upload.gradual.upload_dicom_gradual`.
        """
        return gradual.upload_dicom_gradual(
            self.client,
            source_path,
            project,
            subject,
            session,
            workers=workers,
            direct_archive=direct_archive,
            progress_callback=progress_callback,
        )

    def upload_dicom_gradual_files(
        self,
        *,
        files: Sequence[Path],
        project: str,
        subject: str,
        session: str,
        workers: int = DEFAULT_UPLOAD_WORKERS,
        direct_archive: bool = True,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload a specific list of DICOM files via the gradual-DICOM handler.

        See :func:`xnatctl.services.upload.gradual.upload_dicom_gradual_files`.
        """
        return gradual.upload_dicom_gradual_files(
            self.client,
            files=files,
            project=project,
            subject=subject,
            session=session,
            workers=workers,
            direct_archive=direct_archive,
            progress_callback=progress_callback,
        )

    def upload_resource(
        self,
        session_id: str,
        resource_label: str,
        source_path: Path,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = False,
        overwrite: bool = False,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload files to a resource.

        See :func:`xnatctl.services.upload.resources.upload_resource`.
        """
        return resources.upload_resource(
            self.client,
            session_id,
            resource_label,
            source_path,
            scan_id=scan_id,
            project=project,
            extract=extract,
            overwrite=overwrite,
            progress_callback=progress_callback,
        )

    def _split_into_batches(
        self,
        files: list[Path],
        batch_size: int,
    ) -> Iterator[list[Path]]:
        """Split files into batches.

        Args:
            files: List of file paths.
            batch_size: Maximum files per batch.

        Yields:
            Lists of files for each batch.
        """
        for i in range(0, len(files), batch_size):
            yield files[i : i + batch_size]
