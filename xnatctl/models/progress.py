"""Progress models for tracking operation status.

Provides dataclasses for upload/download progress and operation summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from xnatctl.core.exceptions import BatchOperationError


class OperationPhase(Enum):
    """Operation phases for progress tracking."""

    PREPARING = "preparing"
    ARCHIVING = "archiving"
    UPLOADING = "uploading"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class Progress:
    """Base progress information."""

    phase: OperationPhase
    current: int = 0
    total: int = 0
    message: str = ""
    success: bool = True
    errors: list[str] = field(default_factory=list)

    @property
    def percent(self) -> float:
        """Calculate completion percentage."""
        if self.total == 0:
            return 0.0
        return (self.current / self.total) * 100

    @property
    def is_complete(self) -> bool:
        """Check if operation is complete."""
        return self.phase == OperationPhase.COMPLETE

    @property
    def has_errors(self) -> bool:
        """Check if operation has errors."""
        return len(self.errors) > 0 or self.phase == OperationPhase.ERROR


@dataclass
class UploadProgress(Progress):
    """Upload-specific progress."""

    batch_id: int = 0
    bytes_sent: int = 0
    total_bytes: int = 0
    file_path: str = ""

    @property
    def bytes_percent(self) -> float:
        """Calculate bytes completion percentage."""
        if self.total_bytes == 0:
            return 0.0
        return (self.bytes_sent / self.total_bytes) * 100

    @property
    def mb_sent(self) -> float:
        """Return megabytes sent."""
        return self.bytes_sent / (1024 * 1024)

    @property
    def total_mb(self) -> float:
        """Return total megabytes."""
        return self.total_bytes / (1024 * 1024)


@dataclass
class DownloadProgress(Progress):
    """Download-specific progress."""

    bytes_received: int = 0
    total_bytes: int = 0
    file_path: str = ""
    file_name: str = ""

    @property
    def bytes_percent(self) -> float:
        """Calculate bytes completion percentage."""
        if self.total_bytes == 0:
            return 0.0
        return (self.bytes_received / self.total_bytes) * 100

    @property
    def mb_received(self) -> float:
        """Return megabytes received."""
        return self.bytes_received / (1024 * 1024)

    @property
    def total_mb(self) -> float:
        """Return total megabytes."""
        return self.total_bytes / (1024 * 1024)


@dataclass
class VerificationReport:
    """Result of comparing downloaded files against server-reported checksums.

    Every server-known file lands in exactly one of ``mismatched``,
    ``missing_local``, ``collisions``, or (counted, not listed) ``matched`` --
    ``unverifiable`` additionally flags files the server listed with no digest
    on record, so they are never silently treated as verified. ``collisions``
    covers a key that two different, unrelated files (server-side or
    local-side) both mapped to -- never resolved by silently keeping
    whichever was seen last. ``missing_remote`` covers local files the server
    manifest never mentioned; it does not affect ``success`` -- see the
    property for why.
    """

    matched: int = 0
    mismatched: list[str] = field(default_factory=list)
    missing_local: list[str] = field(default_factory=list)
    missing_remote: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True iff every server-known file matched, with nothing left unresolved.

        A file the server never mentioned (``missing_remote``) does not fail
        verification on its own -- it is not a corrupted or lost download, and
        flagging it would fail on ordinary artifacts like a locally-added note
        file. A run where every checked file landed in ``unverifiable`` (the
        server had no checksums at all) is also not a pass: with nothing
        actually matched, "no mismatches" would otherwise be true vacuously.
        """
        if self.mismatched or self.missing_local or self.collisions:
            return False
        checked = self.matched + len(self.unverifiable)
        return checked == 0 or self.matched > 0


@dataclass
class OperationResult:
    """Generic operation result."""

    success: bool
    total: int
    succeeded: int
    failed: int
    duration: float
    errors: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.total == 0:
            return 100.0
        return (self.succeeded / self.total) * 100


@dataclass
class UploadSummary(OperationResult):
    """Upload operation summary."""

    total_files: int = 0
    total_size_mb: float = 0.0
    batches_total: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    session_id: str = ""
    upload_id: str = ""

    @property
    def throughput_mbps(self) -> float:
        """Calculate upload throughput in MB/s."""
        if self.duration == 0:
            return 0.0
        return self.total_size_mb / self.duration

    def raise_for_status(self) -> None:
        """Raise if the batch upload did not fully succeed.

        The multi-item upload paths report per-item outcomes in a summary rather
        than raising, so a caller that wants a failed batch to stop the program
        calls this, mirroring ``httpx.Response.raise_for_status()``: a no-op on
        success, a typed raise otherwise.

        Raises:
            BatchOperationError: If ``success`` is False, carrying the succeeded
                and failed counts and the per-item error list.
        """
        if self.success:
            return
        raise BatchOperationError("upload", self.succeeded, self.failed, self.errors)


@dataclass
class DownloadSummary(OperationResult):
    """Download operation summary."""

    total_files: int = 0
    total_size_mb: float = 0.0
    output_path: str = ""
    session_id: str = ""
    verification: VerificationReport | None = None

    @property
    def throughput_mbps(self) -> float:
        """Calculate download throughput in MB/s."""
        if self.duration == 0:
            return 0.0
        return self.total_size_mb / self.duration

    def raise_for_status(self) -> None:
        """Raise if the batch download did not fully succeed.

        The multi-item download paths report per-item outcomes in a summary
        rather than raising, so a caller that wants a failed batch to stop the
        program calls this, mirroring ``httpx.Response.raise_for_status()``: a
        no-op on success, a typed raise otherwise.

        Raises:
            BatchOperationError: If ``success`` is False, carrying the succeeded
                and failed counts and the per-item error list.
        """
        if self.success:
            return
        raise BatchOperationError("download", self.succeeded, self.failed, self.errors)
