"""Progress models for tracking operation status.

Provides dataclasses for upload/download progress and operation summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import Field as PydanticField
from pydantic import field_validator

from xnatctl.core.exceptions import BatchOperationError
from xnatctl.core.redact import redact_url_query

from .base import BaseModel


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
    manifest never mentioned; it fails ``success`` only when the manifest
    covered nothing at all -- see the property for why.
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
        The same reasoning covers an EMPTY manifest: with local files on disk
        but the server listing nothing for the scope, nothing was verified,
        and "Verified 0 files" must not read as a pass. Only a manifest and
        local scope that are both empty is trivially fine.
        """
        if self.mismatched or self.missing_local or self.collisions:
            return False
        checked = self.matched + len(self.unverifiable)
        if checked == 0:
            return not self.missing_remote
        return self.matched > 0


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
    skipped_unsafe_entries: int = 0

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


class TransferItemResult(BaseModel):
    """One item (scan, resource, batch, or file) within a `-o json` transfer summary."""

    id: str
    status: Literal["success", "failed"]
    error: str | None = None

    @field_validator("error")
    @classmethod
    def _redact_error(cls, value: str | None) -> str | None:
        """Redact secret-shaped URL query values before *error* ever reaches JSON.

        `error` is frequently `str(exc)` from an arbitrary caught exception
        (a request URL, a server response body, ...), so this is the one
        choke point every construction of a `TransferItemResult` passes
        through -- no call site can forget to redact, because the model
        does it regardless of how the value got here.
        """
        if value is None:
            return value
        return redact_url_query(value)


class TransferVerification(BaseModel):
    """Checksum verification, folded into a transfer summary's `verification` field.

    Field names mirror :class:`VerificationReport` exactly -- this is the
    shape ``session download --verify -o json`` already emitted standalone;
    it now nests under a transfer summary instead of standing alone.
    """

    matched: int = 0
    mismatched: list[str] = PydanticField(default_factory=list)
    missing_local: list[str] = PydanticField(default_factory=list)
    missing_remote: list[str] = PydanticField(default_factory=list)
    unverifiable: list[str] = PydanticField(default_factory=list)
    collisions: list[str] = PydanticField(default_factory=list)

    @classmethod
    def from_report(cls, report: VerificationReport) -> TransferVerification:
        """Build from a service-layer :class:`VerificationReport`, field-for-field."""
        return cls(
            matched=report.matched,
            mismatched=report.mismatched,
            missing_local=report.missing_local,
            missing_remote=report.missing_remote,
            unverifiable=report.unverifiable,
            collisions=report.collisions,
        )


class TransferSummary(BaseModel):
    """Structured `-o json` result for a download/upload transfer command.

    Exactly one of these is printed to stdout at the end of a transfer
    command in JSON mode; progress and success/error text stay on stderr.
    ``status`` always agrees with the process exit code: "success" exits 0,
    "partial" and "failed" exit nonzero.
    """

    operation: Literal["download", "upload"]
    session_id: str | None = None
    project: str | None = None
    output_dir: str | None = None
    source: str | None = None
    scans: int | None = None
    files: int | None = None
    bytes: int | None = None
    duration_seconds: float = 0.0
    status: Literal["success", "partial", "failed"]
    items: list[TransferItemResult] = PydanticField(default_factory=list)
    verification: TransferVerification | None = None
    skipped_unsafe_entries: int = 0

    def emit(self) -> None:
        """Print this summary as the command's single `-o json` stdout object.

        The one call site every transfer command's JSON-mode branch should
        use, so the print mechanics (indentation, `default=str`, ...) live in
        exactly one place.
        """
        # Deferred: core.output imports Rich. TransferSummary is part of the
        # public library surface (xnatctl.__all__) and gets pulled in by a
        # plain data-model consumer that never calls emit() -- a
        # module-scope import here would make Rich a transitive dependency
        # of just holding a TransferSummary.
        from xnatctl.core.output import print_json

        print_json(self.model_dump(mode="json"))


def transfer_status(
    *, succeeded: int, failed: int, success: bool | None = None
) -> Literal["success", "partial", "failed"]:
    """Map a summary's counts (and, where available, its own verdict) to a status.

    *success*, when given, is the underlying summary's own authoritative
    outcome flag (e.g. ``UploadSummary.success``) and takes precedence over
    the counts: a summary that reports failure is never read as "success"
    just because its per-item counters happen to be ``(0, 0)`` -- zero
    attempted is not zero failed. When *success* is ``False``, at least one
    success among *succeeded* downgrades "failed" to "partial"; otherwise
    it's "failed". When *success* is omitted, the verdict is inferred from
    counts alone: all succeeded (or nothing to fail) -> "success"; some
    failed with at least one success -> "partial"; nothing succeeded ->
    "failed".
    """
    if success is True:
        return "success"
    if success is False:
        return "partial" if succeeded > 0 else "failed"
    if failed == 0:
        return "success"
    if succeeded > 0:
        return "partial"
    return "failed"
