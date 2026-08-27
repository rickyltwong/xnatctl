"""Download service for XNAT download operations.

Composes the mixins in the sibling modules into the single public
:class:`DownloadService`, and re-exports the names consumers import from
this package (``xnatctl.services.downloads``) today.
"""

from __future__ import annotations

# ResourceService/SessionService/_safe_output_path are re-exported (via the
# redundant-alias idiom, not __all__ -- they're test-patch targets, not
# public API) because test suites patch them as
# "xnatctl.services.downloads.SessionService.scan_rows" etc. -- resolving
# the class through this module still patches the same class object the
# mixins in .session/.resource_scan/.verification import.
from ..resources import ResourceService as ResourceService
from ..sessions import SessionService as SessionService
from .resource_scan import _ResourceScanDownloadMixin
from .session import DownloadOutcome, ScanResult, _SessionDownloadMixin
from .shared import _safe_output_path as _safe_output_path
from .transport import StreamedFile, stream_to_file
from .verification import _VerificationMixin

__all__ = [
    "DownloadOutcome",
    "DownloadService",
    "ScanResult",
    "StreamedFile",
    "stream_to_file",
]


class DownloadService(
    _SessionDownloadMixin,
    _ResourceScanDownloadMixin,
    _VerificationMixin,
):
    """Service for XNAT download operations."""
