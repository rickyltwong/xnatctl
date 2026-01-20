"""Progress models for tracking operation status.

Provides dataclasses for upload/download progress and operation summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


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
    errors: List[str] = field(default_factory=list)

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
class OperationResult:
    """Generic operation result."""

    success: bool
    total: int
    succeeded: int
    failed: int
    duration: float
    errors: List[str] = field(default_factory=list)

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


@dataclass
class DownloadSummary(OperationResult):
    """Download operation summary."""

    total_files: int = 0
    total_size_mb: float = 0.0
    output_path: str = ""
    session_id: str = ""
    verified: bool = False

    @property
    def throughput_mbps(self) -> float:
        """Calculate download throughput in MB/s."""
        if self.duration == 0:
            return 0.0
        return self.total_size_mb / self.duration
