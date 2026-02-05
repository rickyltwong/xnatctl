"""DICOM C-STORE uploader using pynetdicom.

This module provides DICOM network transfer capability for sending
DICOM files to an XNAT DICOM SCP receiver.

Requires the optional 'dicom' extras: pip install xnatctl[dicom]

This is an internal implementation detail. Use `UploadService` from
`xnatctl.services.uploads` as the public API.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from xnatctl.uploaders.common import collect_dicom_files, split_into_n_batches
from xnatctl.uploaders.constants import (
    DEFAULT_DICOM_CALLING_AET,
    DEFAULT_DICOM_PORT,
    DEFAULT_DICOM_STORE_WORKERS,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Lazy Imports (pynetdicom/pydicom are optional)
# =============================================================================


def _check_dicom_deps() -> None:
    """Check if DICOM dependencies are available.

    Raises:
        ImportError: If pydicom or pynetdicom are not installed.
    """
    try:
        import pydicom  # noqa: F401
        import pynetdicom  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "DICOM C-STORE requires pydicom and pynetdicom. "
            "Install with: pip install xnatctl[dicom]"
        ) from e


def _get_verification_sop_class():
    """Get VerificationSOPClass with compatibility for pynetdicom versions."""
    from pynetdicom import sop_class as _sop_class

    VERIFICATION_UID = "1.2.840.10008.1.1"
    return getattr(
        _sop_class,
        "VerificationSOPClass",
        getattr(_sop_class, "Verification", VERIFICATION_UID),
    )


def _get_storage_contexts():
    """Get storage presentation contexts with version compatibility."""
    try:
        from pynetdicom import StoragePresentationContexts

        return list(StoragePresentationContexts)
    except ImportError:
        from pynetdicom import sop_class as _sc
        from pynetdicom.presentation import build_context

        uids = [getattr(_sc, name) for name in dir(_sc) if name.endswith("Storage")]
        return [build_context(uid) for uid in uids]


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class DICOMStoreSummary:
    """Summary of a DICOM C-STORE operation."""

    total_files: int
    sent: int
    failed: int
    log_dir: Path
    workspace: Path
    success: bool


# =============================================================================
# Utility Functions
# =============================================================================


def ensure_sop_uids(ds) -> None:
    """Populate missing SOP UID attributes from file-meta.

    Some DICOM files may lack SOPClassUID/SOPInstanceUID in the dataset
    but have them in the file-meta header.

    Args:
        ds: pydicom Dataset object.
    """
    if not getattr(ds, "SOPClassUID", None):
        uid = getattr(ds.file_meta, "MediaStorageSOPClassUID", None)
        if uid:
            ds.SOPClassUID = uid

    if not getattr(ds, "SOPInstanceUID", None):
        uid = getattr(ds.file_meta, "MediaStorageSOPInstanceUID", None)
        if uid:
            ds.SOPInstanceUID = uid


def c_echo(host: str, port: int, calling_aet: str, called_aet: str) -> bool:
    """Send a C-ECHO to verify connectivity and AE titles.

    Args:
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.

    Returns:
        True if C-ECHO succeeded.
    """
    _check_dicom_deps()
    from pynetdicom import AE

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(_get_verification_sop_class())

    assoc = ae.associate(host, port, ae_title=called_aet)
    if not assoc.is_established:
        return False

    status = assoc.send_c_echo()
    assoc.release()

    return bool(status and status.Status == 0x0000)


def send_batch(
    batch_id: str,
    files: list[Path],
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    log_dir: Path,
) -> tuple[int, int]:
    """Send a batch of DICOM files over a single association.

    Args:
        batch_id: Identifier for this batch (for logging).
        files: List of DICOM file paths.
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        log_dir: Directory for batch log files.

    Returns:
        Tuple of (sent_count, failed_count).
    """
    _check_dicom_deps()
    import pydicom
    from pydicom.errors import InvalidDicomError
    from pynetdicom import AE

    sent = failed = 0
    log_path = log_dir / f"{batch_id}.log"

    with log_path.open("w") as log:
        ae = AE(ae_title=calling_aet)
        ae.requested_contexts = _get_storage_contexts()
        # Add Siemens private SOP class
        ae.add_requested_context("1.3.12.2.1107.5.9.1")

        assoc = ae.associate(host, port, ae_title=called_aet)
        if not assoc.is_established:
            log.write("Association rejected/aborted\n")
            return sent, len(files)

        for file_path in files:
            try:
                ds = pydicom.dcmread(file_path, force=True)
            except InvalidDicomError:
                failed += 1
                log.write(f"Skip non-DICOM {file_path}\n")
                continue

            ensure_sop_uids(ds)

            try:
                status = assoc.send_c_store(ds)
            except Exception as e:
                failed += 1
                log.write(f"Store error {file_path}: {type(e).__name__}: {e}\n")
                continue

            if status and status.Status == 0x0000:
                sent += 1
            else:
                failed += 1
                status_hex = hex(status.Status) if status else "0x0000"
                log.write(f"Failed {file_path} status {status_hex}\n")

        assoc.release()

    return sent, failed


# =============================================================================
# Main Function
# =============================================================================


def send_dicom_store(
    *,
    dicom_root: Path,
    host: str,
    port: int = DEFAULT_DICOM_PORT,
    called_aet: str,
    calling_aet: str = DEFAULT_DICOM_CALLING_AET,
    workers: int = DEFAULT_DICOM_STORE_WORKERS,
    cleanup: bool = True,
) -> DICOMStoreSummary:
    """Send DICOM files to an SCP using C-STORE.

    This function:
    1. Verifies connectivity with C-ECHO
    2. Collects DICOM files from the root directory
    3. Splits files into batches for parallel associations
    4. Sends files using multiple concurrent C-STORE associations

    Args:
        dicom_root: Directory containing DICOM files.
        host: DICOM SCP host.
        port: DICOM SCP port (default: 104).
        called_aet: Remote AE title.
        calling_aet: Our AE title (default: XNATCTL).
        workers: Number of parallel associations (default: 4).
        cleanup: Remove temporary workspace on completion (default: True).

    Returns:
        DICOMStoreSummary with results.

    Raises:
        ImportError: If pydicom/pynetdicom are not installed.
        ValueError: If dicom_root is not a directory.
        RuntimeError: If C-ECHO fails or no DICOM files found.
    """
    _check_dicom_deps()

    if not dicom_root.exists() or not dicom_root.is_dir():
        raise ValueError(f"dicom_root is not a directory: {dicom_root}")

    # Create workspace for logs
    workspace = Path(tempfile.mkdtemp(prefix="xnatctl_dicom_store_"))
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Verify connectivity
        logger.info(
            "Pre-flight C-ECHO %s -> %s @ %s:%s",
            calling_aet,
            called_aet,
            host,
            port,
        )
        if not c_echo(host, port, calling_aet, called_aet):
            raise RuntimeError(
                f"C-ECHO failed - check host/port/AET settings "
                f"(host={host}, port={port}, called_aet={called_aet})"
            )

        # Collect files
        files = collect_dicom_files(dicom_root)
        if not files:
            raise RuntimeError(f"No DICOM files found in {dicom_root}")

        # Split into batches
        batches = split_into_n_batches(files, workers)
        logger.info(
            "Discovered %d files, using %d parallel associations",
            len(files),
            len(batches),
        )

        # Send batches in parallel
        sent_total = 0
        failed_total = 0

        with ThreadPoolExecutor(max_workers=len(batches)) as pool:
            futures = {
                pool.submit(
                    send_batch,
                    f"{i:03d}",
                    batch,
                    host,
                    port,
                    calling_aet,
                    called_aet,
                    log_dir,
                ): i
                for i, batch in enumerate(batches)
            }

            for future in as_completed(futures):
                batch_idx = futures[future]
                sent, failed = future.result()
                sent_total += sent
                failed_total += failed
                logger.info(
                    "Batch %03d complete: %d sent, %d failed",
                    batch_idx,
                    sent,
                    failed,
                )

        return DICOMStoreSummary(
            total_files=len(files),
            sent=sent_total,
            failed=failed_total,
            log_dir=log_dir,
            workspace=workspace,
            success=failed_total == 0,
        )

    finally:
        # Only cleanup on success - preserve logs for debugging failures
        if cleanup and failed_total == 0:
            shutil.rmtree(workspace, ignore_errors=True)
