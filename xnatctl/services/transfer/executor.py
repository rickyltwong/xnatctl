"""Transfer executor for moving data between XNAT instances.

Handles the actual HTTP operations: creating subjects, per-scan downloads,
DICOM-zip imports with retry, non-DICOM resource uploads, and ZIP validation.
``TransferExecutor`` itself is assembled from cohesion-split mixins that live
in this package: destination hierarchy CRUD and source discovery
(``executor_hierarchy``), DICOM scan transfer (``executor_dicom``), non-DICOM
resource transfer (``executor_resources``), and prearchive/archive polling
(``executor_archive``). ``_retryable_import_failure`` is re-exported here for
callers/tests that import it from this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xnatctl.services.transfer.executor_archive import _ArchiveMixin
from xnatctl.services.transfer.executor_dicom import _DicomTransferMixin, _retryable_import_failure
from xnatctl.services.transfer.executor_hierarchy import _HierarchyMixin
from xnatctl.services.transfer.executor_resources import _ResourceTransferMixin

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

__all__ = ["TransferExecutor", "_retryable_import_failure"]


class TransferExecutor(
    _HierarchyMixin,
    _DicomTransferMixin,
    _ResourceTransferMixin,
    _ArchiveMixin,
):
    """Execute individual transfer operations between two XNAT instances.

    Args:
        source_client: Authenticated source XNATClient.
        dest_client: Authenticated destination XNATClient.
    """

    def __init__(self, source_client: XNATClient, dest_client: XNATClient) -> None:
        self.source = source_client
        self.dest = dest_client
