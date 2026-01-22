"""XNAT upload transports for xnatctl.

This module provides upload implementations for DICOM data:
- Parallel REST uploader (batched archive uploads via REST API)
- DICOM C-STORE uploader (pynetdicom-based network transfer)

These are internal implementation details. Use `UploadService` from
`xnatctl.services.uploads` as the public API.
"""

from xnatctl.uploaders.common import (
    DICOM_EXTENSIONS,
    collect_dicom_files,
    split_into_batches,
    split_into_n_batches,
)
from xnatctl.uploaders.constants import (
    DEFAULT_ARCHIVE_FORMAT,
    DEFAULT_ARCHIVE_WORKERS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_DICOM_CALLING_AET,
    DEFAULT_DICOM_PORT,
    DEFAULT_DICOM_STORE_WORKERS,
    DEFAULT_IMPORT_HANDLER,
    DEFAULT_OVERWRITE,
    DEFAULT_TIMEOUT,
    DEFAULT_UPLOAD_WORKERS,
)
from xnatctl.uploaders.parallel_rest import (
    UploadProgress,
    UploadResult,
    UploadSummary,
    upload_dicom_parallel_rest,
)

# DICOM C-STORE imports are lazy to avoid requiring pynetdicom
# Use: from xnatctl.uploaders.dicom_store import send_dicom_store

__all__ = [
    # Constants
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
    # Common utilities
    "DICOM_EXTENSIONS",
    "collect_dicom_files",
    "split_into_batches",
    "split_into_n_batches",
    # REST uploader
    "UploadProgress",
    "UploadResult",
    "UploadSummary",
    "upload_dicom_parallel_rest",
]
