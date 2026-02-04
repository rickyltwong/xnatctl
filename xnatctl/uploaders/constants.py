"""Shared constants for uploader modules.

These defaults are conservative for broad compatibility. For high-throughput
environments with fast storage and network, consider increasing workers via
CLI flags (e.g., --upload-workers 16 --archive-workers 8).
"""

from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS

# =============================================================================
# REST Upload Defaults (conservative)
# =============================================================================

# Files per batch (internal semantic for splitting)
DEFAULT_BATCH_SIZE = 500

# Parallel workers for upload operations
DEFAULT_UPLOAD_WORKERS = 4

# Parallel workers for archive creation
DEFAULT_ARCHIVE_WORKERS = 4

# Archive format: "tar" or "zip"
DEFAULT_ARCHIVE_FORMAT = "tar"

# HTTP timeout for upload requests (6 hours for large datasets)
DEFAULT_TIMEOUT = DEFAULT_HTTP_TIMEOUT_SECONDS

# XNAT import handler
DEFAULT_IMPORT_HANDLER = "DICOM-zip"

# Overwrite mode: "none", "append", or "delete"
DEFAULT_OVERWRITE = "delete"

# =============================================================================
# DICOM C-STORE Defaults
# =============================================================================

# Parallel workers for C-STORE batches
DEFAULT_DICOM_STORE_WORKERS = 4

# Default calling AE title
DEFAULT_DICOM_CALLING_AET = "XNATCTL"

# Default DICOM SCP port
DEFAULT_DICOM_PORT = 104

# =============================================================================
# High-Throughput Recommendations (not defaults)
# =============================================================================
# For high-IO hosts with fast storage and network:
#   --upload-workers 16-42
#   --archive-workers 8-16
#   --batch-size 200-500
#
# These are NOT defaults to avoid overwhelming modest systems or XNAT servers.
