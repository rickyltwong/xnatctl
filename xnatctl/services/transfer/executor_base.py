"""Typed attribute contract shared by the ``TransferExecutor`` mixin modules.

``TransferExecutor`` itself (in :mod:`xnatctl.services.transfer.executor`) is
assembled from mixins split across this package by cohesion (hierarchy CRUD,
DICOM scan transfer, non-DICOM resource transfer, and prearchive/archive
polling). Each mixin subclasses :class:`_ExecutorAttrs` so mypy resolves
``self.source``/``self.dest`` -- and the handful of methods one mixin calls
on another -- without every mixin redeclaring them. The attributes are
actually assigned once, in ``TransferExecutor.__init__``; the methods are
actually implemented once each, in their owning mixin.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient


class _ExecutorAttrs:
    """Declares cross-mixin attributes/methods shared by all executor mixins."""

    source: XNATClient
    dest: XNATClient

    def validate_zip(self, zip_path: Path) -> bool:
        """Declared here; implemented by ``_ResourceTransferMixin``."""
        raise NotImplementedError

    def count_dest_scans(self, dest_project: str, subject_label: str, experiment_label: str) -> int:
        """Declared here; implemented by ``_HierarchyMixin``."""
        raise NotImplementedError
