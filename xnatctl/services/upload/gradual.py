"""Gradual-DICOM per-file upload transport.

Uploads files one at a time through the ``gradual-DICOM`` import handler, in
parallel across worker threads, with a warm-up phase, a bounded-concurrency
salvage pass, and a final sequential retry for stragglers.

``GradualUploadRun`` (see :mod:`gradual_client` for the HTTP client pool it
wraps) carries the report/display/scan-grouping/submit helpers as methods:
each phase (warm-up, main parallel pass, salvage pass, final retry) is an
independently readable method holding no module-level mutable state.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken, cancellable_pool
from xnatctl.core.client import XNATClient
from xnatctl.core.server_version import MIN_VERSION_DIRECT_ARCHIVE, require_server_version
from xnatctl.models.progress import OperationPhase, UploadProgress, UploadSummary

from .gradual_client import GradualClientPool, _upload_single_file_gradual
from .shared import (
    DEFAULT_UPLOAD_WORKERS,
    SessionRefresher,
    _error_signature,
    _is_dicom_like_path,
    collect_dicom_files,
)

logger = logging.getLogger(__name__)


@dataclass
class GradualUploadRun:
    """Shared state for one gradual-DICOM upload operation.

    Holds the report/display/scan-grouping/submit helpers as methods, so
    each phase (warm-up, main parallel pass, salvage pass, final retry) is
    independently readable and testable.
    """

    client: XNATClient
    pool: GradualClientPool
    project: str
    subject: str
    session: str
    direct_archive: bool
    display_root: Path
    progress_callback: Callable[[UploadProgress], None] | None
    start_time: float

    session_refresher: SessionRefresher = field(init=False)
    completed: int = field(init=False, default=0)
    failed_paths: set[Path] = field(init=False, default_factory=set)
    error_by_path: dict[Path, str] = field(init=False, default_factory=dict)
    total_files: int = field(init=False, default=0)
    # Set once the first run() call has started. Lets a second run() on a
    # reused instance re-stamp start_time (so its duration is not inflated by
    # everything since construction) without disturbing the first call, whose
    # constructor-supplied start_time deliberately predates run() -- it is set
    # before this run's files were even scanned/extracted, and that scan time
    # belongs in the reported duration.
    _has_run: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        self.session_refresher = SessionRefresher(
            base_url=self.client.base_url,
            verify_ssl=self.client.httpx_verify(),
            token=self.client.session_token,
            username=self.client.username,
            password=self.client.password,
            # So the phases that run after this upload inherit the refreshed
            # token instead of the one it started with.
            owner=self.client,
        )

    def report(self, phase: OperationPhase, **kwargs: Any) -> None:
        """Emit a progress event, if a callback was given."""
        if self.progress_callback:
            self.progress_callback(UploadProgress(phase=phase, **kwargs))

    def display(self, path: Path) -> str:
        """Path shown in progress/error messages, relative to the upload root."""
        try:
            return str(path.relative_to(self.display_root))
        except ValueError:
            return path.name

    def _upload_one(
        self, path: Path, cancel_token: CancellationToken = NULL_TOKEN
    ) -> tuple[str, bool, str]:
        return _upload_single_file_gradual(
            pool=self.pool,
            base_url=self.client.base_url,
            session_refresher=self.session_refresher,
            verify_ssl=self.client.httpx_verify(),
            file_path=path,
            display_path=self.display(path),
            project=self.project,
            subject=self.subject,
            session=self.session,
            direct_archive=self.direct_archive,
            cancel_token=cancel_token,
        )

    def _scan_id_for(self, path: Path) -> str | None:
        """Extract scan ID from standard session layout paths, if present."""
        try:
            rel = path.relative_to(self.display_root)
        except ValueError:
            return None
        parts = rel.parts
        # Expected layout: scans/<scan_id>/resources/DICOM/files/<...>
        if (
            len(parts) >= 6
            and parts[0] == "scans"
            and parts[2] == "resources"
            and parts[3] == "DICOM"
            and parts[4] == "files"
        ):
            return parts[1]
        return None

    @staticmethod
    def _scan_sort_key(scan_id: str) -> tuple[int, int, str]:
        try:
            return (0, int(scan_id), scan_id)
        except ValueError:
            return (1, 0, scan_id)

    def _partition_warmup(self, file_list: list[Path]) -> tuple[list[Path], list[Path]]:
        """Pick a small warm-up set before going wide-parallel.

        XNAT can return transient HTTP 400s when a session/scan is being
        created in prearchive. With high concurrency, multiple workers can
        hit that "cold start" race at the same time.
        """
        scan_groups: dict[str, list[Path]] = {}
        other_files: list[Path] = []
        for p in file_list:
            sid = self._scan_id_for(p)
            if sid:
                scan_groups.setdefault(sid, []).append(p)
            else:
                other_files.append(p)

        warmup_files: list[Path] = []
        remaining_files: list[Path] = []

        if scan_groups:
            # Warm up one file per scan (capped) and interleave remaining uploads
            # across scans to reduce per-scan contention under high worker counts.
            queues: dict[str, deque[Path]] = {
                sid: deque(paths) for sid, paths in scan_groups.items()
            }
            if other_files:
                queues["_other"] = deque(other_files)

            scan_ids = sorted(queues.keys(), key=self._scan_sort_key)
            max_warmup_scans = min(50, len(scan_ids))
            warmup_scan_ids = [sid for sid in scan_ids if sid != "_other"][:max_warmup_scans]

            for sid in warmup_scan_ids:
                q = queues.get(sid)
                if q:
                    warmup_files.append(q.popleft())

            # Round-robin remaining files across scan queues
            scan_order = deque(scan_ids)
            while scan_order:
                sid = scan_order.popleft()
                q = queues.get(sid)
                if not q:
                    queues.pop(sid, None)
                    continue
                remaining_files.append(q.popleft())
                if q:
                    scan_order.append(sid)
                else:
                    queues.pop(sid, None)
        else:
            # Fallback: warm up a few files in provided order
            warmup_n = min(5, self.total_files)
            warmup_files = file_list[:warmup_n]
            remaining_files = file_list[warmup_n:]

        return warmup_files, remaining_files

    def _run_warmup(self, warmup_files: list[Path]) -> None:
        """Upload the warm-up set sequentially.

        No cancel_token here on purpose: the warmup is sequential and runs on
        the main thread, so Ctrl+C interrupts its retry sleep directly. The
        token only earns its place where work happens in worker threads.
        """
        for p in warmup_files:
            _name, ok, err = self._upload_one(p)
            self.completed += 1
            if not ok:
                self.failed_paths.add(p)
                self.error_by_path[p] = err

            succeeded_so_far = self.completed - len(self.failed_paths)
            self.report(
                OperationPhase.UPLOADING,
                current=self.completed,
                total=self.total_files,
                success=ok,
                message=(
                    f"Uploaded {self.completed}/{self.total_files} "
                    f"({succeeded_so_far} ok, {len(self.failed_paths)} failed)"
                ),
            )

    def _warmup_circuit_breaker(self, warmup_files: list[Path]) -> UploadSummary | None:
        """Abort early when the server rejected every warm-up file identically.

        The warmup exists to try a small, deterministic set before opening the
        throttle; if the server refused every one of them for the same reason,
        the remaining files will be refused for that reason too. Stopping here
        is the difference between one round of errors and hours of them: the
        wide-parallel phase plus two salvage passes would otherwise retry
        every file in a 100k-file directory before reporting the same message.
        """
        if not (warmup_files and len(self.failed_paths) == len(warmup_files)):
            return None

        reasons = {_error_signature(self.error_by_path.get(p, "")) for p in warmup_files}
        if len(reasons) != 1:
            return None

        reason = self.error_by_path.get(warmup_files[0], "").strip()
        message = (
            f"Server rejected all {len(warmup_files)} warmup files with the same "
            f"error, so the remaining {self.total_files - len(warmup_files)} would fail "
            f"the same way: {reason}. Check the project, subject and session "
            f"labels before retrying."
        )
        logger.error("Aborting gradual upload: %s", message)
        self.report(OperationPhase.ERROR, message=message, success=False, errors=[message])
        return UploadSummary(
            success=False,
            total=self.total_files,
            succeeded=0,
            failed=len(warmup_files),
            duration=time.time() - self.start_time,
            errors=[message],
            session_id=self.session,
        )

    def _run_main_pass(self, remaining_files: list[Path], workers: int) -> CancellationToken:
        """Parallel per-file upload with a bounded in-flight window.

        Returns the cancel token used, so the salvage pass can share it and
        honour a Ctrl+C that happened during this phase.
        """
        with cancellable_pool(workers) as (executor, gradual_token):
            prefetch = max(1, workers * 2)
            file_iter = iter(remaining_files)

            in_flight: set[Future[tuple[str, bool, str]]] = set()
            future_to_path: dict[Future[tuple[str, bool, str]], Path] = {}

            def _submit_one(path: Path) -> None:
                # This pass refills its window as files complete, so without
                # this check a cancelled run would keep feeding itself the rest
                # of the directory one file at a time.
                if gradual_token.cancelled:
                    raise StopIteration
                fut: Future[tuple[str, bool, str]] = executor.submit(
                    self._upload_one, path, gradual_token
                )
                in_flight.add(fut)
                future_to_path[fut] = path

            for _ in range(min(prefetch, len(remaining_files))):
                try:
                    _submit_one(next(file_iter))
                except StopIteration:
                    break

            while in_flight:
                done, _pending = wait(in_flight, return_when=FIRST_COMPLETED)
                in_flight = _pending

                for future in done:
                    self.completed += 1
                    p = future_to_path.pop(future)

                    try:
                        _name, ok, err = future.result()
                    except Exception as e:  # noqa: BLE001  # worker-pool isolation: future.result() can re-raise anything the upload task raised
                        ok = False
                        err = str(e)

                    if not ok:
                        self.failed_paths.add(p)
                        self.error_by_path[p] = err

                    succeeded_so_far = self.completed - len(self.failed_paths)
                    self.report(
                        OperationPhase.UPLOADING,
                        current=self.completed,
                        total=self.total_files,
                        success=ok,
                        message=(
                            f"Uploaded {self.completed}/{self.total_files} "
                            f"({succeeded_so_far} ok, {len(self.failed_paths)} failed)"
                        ),
                    )

                    while len(in_flight) < prefetch:
                        try:
                            _submit_one(next(file_iter))
                        except StopIteration:
                            break

        return gradual_token

    def _run_salvage_pass(self, workers: int, gradual_token: CancellationToken) -> None:
        """Retry a bounded number of failed files at lower concurrency.

        This helps when XNAT returns transient 400s under high parallel load.
        """
        max_salvage = min(5000, max(500, int(self.total_files * 0.01)))
        if not (self.failed_paths and len(self.failed_paths) <= max_salvage):
            return

        retry_workers = max(1, min(4, workers))
        self.report(
            OperationPhase.PREPARING,
            message=(
                f"Retrying {len(self.failed_paths)} failed file(s) "
                f"at lower concurrency ({retry_workers} workers)..."
            ),
        )

        to_retry = sorted(self.failed_paths, key=self.display)
        remaining_failed: set[Path] = set(self.failed_paths)

        with cancellable_pool(retry_workers, gradual_token) as (retry_executor, _):
            prefetch = max(1, retry_workers * 2)
            retry_iter = iter(to_retry)
            retry_in_flight: set[Future[tuple[str, bool, str]]] = set()
            retry_future_to_path: dict[Future[tuple[str, bool, str]], Path] = {}

            def _submit_retry(path: Path) -> None:
                # Same two guards as the main pass. Without them the
                # salvage pass ran on NULL_TOKEN: after Ctrl+C every
                # in-flight file sat out its full 2+4+8+16+32s retry
                # ladder before shutdown(wait=True) could return -- the
                # exact wait cooperative cancellation exists to remove,
                # reintroduced in the one pass whose files are already
                # known to be failing.
                if gradual_token.cancelled:
                    raise StopIteration
                fut: Future[tuple[str, bool, str]] = retry_executor.submit(
                    self._upload_one, path, gradual_token
                )
                retry_in_flight.add(fut)
                retry_future_to_path[fut] = path

            for _ in range(min(prefetch, len(to_retry))):
                try:
                    _submit_retry(next(retry_iter))
                except StopIteration:
                    break

            while retry_in_flight:
                done, _pending = wait(retry_in_flight, return_when=FIRST_COMPLETED)
                retry_in_flight = _pending

                for future in done:
                    p = retry_future_to_path.pop(future)
                    try:
                        _name, ok, err = future.result()
                    except Exception as e:  # noqa: BLE001  # worker-pool isolation: future.result() re-raise, salvage pass
                        ok = False
                        err = str(e)

                    if ok:
                        remaining_failed.discard(p)
                        self.error_by_path.pop(p, None)
                    else:
                        self.error_by_path[p] = err

                    while len(retry_in_flight) < prefetch:
                        try:
                            _submit_retry(next(retry_iter))
                        except StopIteration:
                            break

        self.failed_paths = remaining_failed

    def _run_final_retry(self) -> None:
        """Sequential last-chance retry when only a handful of files remain failed."""
        if not (self.failed_paths and len(self.failed_paths) <= 50):
            return

        self.report(
            OperationPhase.PREPARING,
            message=f"Final sequential retry for {len(self.failed_paths)} file(s)...",
        )

        remaining_failed: set[Path] = set()
        for p in sorted(self.failed_paths, key=self.display):
            _name, ok, err = self._upload_one(p)
            if ok:
                self.error_by_path.pop(p, None)
            else:
                remaining_failed.add(p)
                self.error_by_path[p] = err

        self.failed_paths = remaining_failed

    def _finish(self) -> UploadSummary:
        duration = time.time() - self.start_time
        failed = len(self.failed_paths)
        succeeded = self.total_files - failed
        overall_success = failed == 0

        errors = [
            f"{self.display(p)}: {self.error_by_path.get(p, '')}".rstrip(": ")
            for p in sorted(self.failed_paths, key=self.display)
        ]

        self.report(
            OperationPhase.COMPLETE if overall_success else OperationPhase.ERROR,
            current=self.total_files,
            total=self.total_files,
            message=(
                f"Uploaded {succeeded} files via gradual-DICOM"
                if overall_success
                else f"Uploaded {succeeded}/{self.total_files} files ({failed} failed)"
            ),
            success=overall_success,
            errors=errors,
        )

        return UploadSummary(
            success=overall_success,
            total=self.total_files,
            succeeded=succeeded,
            failed=failed,
            duration=duration,
            errors=errors,
            total_files=self.total_files,
            session_id=self.session,
        )

    def run(self, files: Sequence[Path], workers: int) -> UploadSummary:
        """Upload every file in *files*, returning the completed summary.

        Gates on ``direct_archive`` before any archive creation or network
        upload -- this is the canonical boundary for that check.
        ``upload_dicom_gradual``/``upload_dicom_gradual_files`` construct and
        run an instance after their own local validation (empty file list,
        missing source, no DICOM files found), so gating here rather than in
        those functions naturally keeps the gate after local validation and
        before any network work. It also means a library caller that
        constructs and runs ``GradualUploadRun`` directly -- it is exported
        for that -- cannot send a direct-archive import to an unsupported
        server ungated, since there is no other, un-gated boundary left to
        reach the network from.

        Resets every accumulator a previous ``run()`` on this instance would
        have left behind (``completed``, ``failed_paths``, ``error_by_path``,
        ``total_files``) so a reused instance starts clean rather than
        retrying stale failures or reporting impossible progress against the
        new file count. A second (or later) call also re-stamps
        ``start_time``, so its reported duration covers only that call rather
        than accumulating from the instance's construction; the first call
        keeps the constructor-supplied ``start_time`` untouched, since callers
        set it before scanning/extracting *files* and that time belongs in
        the first run's duration.

        Enters this run's own scope on the client pool: ``UploadService``
        always wraps its gradual calls in an outer ``pool.scope()``, but nothing
        stops ``GradualUploadRun`` from being constructed and run directly, and
        without a scope of its own here the pool's HTTP clients would never be
        closed. The scope is refcounted, so nesting under an outer one is safe.

        Raises:
            UnsupportedServerVersionError: If ``direct_archive`` is set and
                the server is known to be older than
                :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`.
        """
        if self.direct_archive:
            require_server_version(self.client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

        if self._has_run:
            self.start_time = time.time()
        self._has_run = True

        self.completed = 0
        self.failed_paths = set()
        self.error_by_path = {}

        with self.pool.scope():
            file_list = list(files)
            self.total_files = len(file_list)

            self.report(
                OperationPhase.PREPARING,
                total=self.total_files,
                message=f"Found {self.total_files} files for gradual-DICOM upload",
            )

            warmup_files, remaining_files = self._partition_warmup(file_list)
            if warmup_files:
                self.report(
                    OperationPhase.PREPARING,
                    message=(
                        f"Warming up gradual-DICOM upload with {len(warmup_files)} file(s)..."
                    ),
                )

            self._run_warmup(warmup_files)

            aborted = self._warmup_circuit_breaker(warmup_files)
            if aborted is not None:
                return aborted

            gradual_token = self._run_main_pass(remaining_files, workers)
            self._run_salvage_pass(workers, gradual_token)
            self._run_final_retry()

            return self._finish()


def _stage_source_files(source_path: Path) -> tuple[str | None, list[Path]]:
    """Resolve an upload source into DICOM-like files, extracting ZIPs first.

    ZIP sources are extracted into a fresh temp directory that the caller must
    remove after the upload; on a mid-extraction failure the directory is
    removed here before the error propagates, so the caller never has to
    clean up a half-staged tree it was never told about.

    Args:
        source_path: Existing directory or ZIP file.

    Returns:
        Tuple of (temp extraction dir or None, DICOM-like files found).

    Raises:
        ValueError: If source_path is neither a directory nor a ZIP file.
    """
    if source_path.is_file() and source_path.suffix.lower() == ".zip":
        temp_dir = tempfile.mkdtemp(prefix="xnatctl_gradual_")
        try:
            temp_path = Path(temp_dir)
            with zipfile.ZipFile(source_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    target = (temp_path / member.filename).resolve()
                    if not target.is_relative_to(temp_path.resolve()):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            return temp_dir, collect_dicom_files(temp_path)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
    if source_path.is_dir():
        return None, collect_dicom_files(source_path)
    raise ValueError("gradual-DICOM requires a directory or ZIP file")


def upload_dicom_gradual(
    client: XNATClient,
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

    Each file is uploaded individually to the XNAT import service using
    the gradual-DICOM handler, which lets XNAT parse each file on ingest.
    Files are uploaded in parallel using per-thread HTTP clients.

    Accepts directories or ZIP archives. ZIP archives are extracted to a
    temporary directory before upload. Only DICOM-like files are sent:
    known DICOM extensions plus extensionless files commonly produced by
    scanners.

    Args:
        client: Bound XNAT client.
        source_path: Directory or ZIP file containing DICOM files.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.
        workers: Number of parallel upload workers (default: 4).
        direct_archive: Use direct archive vs prearchive (default: True).
        progress_callback: Optional callback for progress updates.

    Returns:
        UploadSummary with results.

    Raises:
        ValueError: If source_path is not a directory or ZIP file.
        FileNotFoundError: If source_path does not exist.
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`
            (raised by :meth:`GradualUploadRun.run`, after the local checks
            below).
    """
    pool = GradualClientPool()
    with pool.scope():
        start_time = time.time()
        source_path = Path(source_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        temp_dir, files = _stage_source_files(source_path)

        try:
            if not files:
                return UploadSummary(
                    success=False,
                    total=0,
                    succeeded=0,
                    failed=0,
                    duration=time.time() - start_time,
                    errors=["No DICOM files found"],
                )

            # Prefer stable relative paths in logs/errors (especially for ZIP
            # extractions into a temp directory).
            display_root = Path(temp_dir) if temp_dir else source_path

            run = GradualUploadRun(
                client=client,
                pool=pool,
                project=project,
                subject=subject,
                session=session,
                direct_archive=direct_archive,
                display_root=display_root,
                progress_callback=progress_callback,
                start_time=start_time,
            )
            return run.run(files, workers)

        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)


def _validate_files_exist(file_list: list[Path]) -> None:
    """Reject a file list containing missing paths or non-files.

    Args:
        file_list: Caller-provided paths.

    Raises:
        FileNotFoundError: If any path does not exist.
        ValueError: If any path is not a file.
    """
    for p in file_list:
        if not p.exists():
            raise FileNotFoundError(f"File not found: {p}")
        if not p.is_file():
            raise ValueError(f"Not a file: {p}")


def _reject_duplicate_resolved_paths(dicom_file_list: list[Path]) -> None:
    """Reject distinct input paths that resolve to the same file on disk.

    Args:
        dicom_file_list: DICOM-like files selected for upload.

    Raises:
        ValueError: If two paths resolve to the same file.
    """
    resolved_to_original: dict[Path, Path] = {}
    duplicate_resolved: set[Path] = set()
    for p in dicom_file_list:
        resolved = p.expanduser().resolve(strict=False)
        if resolved in resolved_to_original:
            duplicate_resolved.add(resolved)
        else:
            resolved_to_original[resolved] = p

    if duplicate_resolved:
        dup_str = ", ".join(sorted(str(p) for p in duplicate_resolved))
        raise ValueError(f"Duplicate file paths provided: {dup_str}")


def _compute_display_root(dicom_file_list: list[Path]) -> Path:
    """Pick a stable common root for relative display paths.

    Args:
        dicom_file_list: DICOM-like files selected for upload.

    Returns:
        The deepest common directory, falling back to the first file's
        parent when no common path exists.
    """
    try:
        common = Path(os.path.commonpath([str(p.resolve()) for p in dicom_file_list]))
        return common if common.is_dir() else common.parent
    except (ValueError, OSError):
        return dicom_file_list[0].parent


def upload_dicom_gradual_files(
    client: XNATClient,
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

    Unlike :func:`upload_dicom_gradual`, this uploads only the files
    explicitly provided and does not scan any directories.

    Args:
        client: Bound XNAT client.
        files: Explicit list of files to upload.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.
        direct_archive: Use direct archive vs prearchive (default: True).
        workers: Number of parallel upload workers.
        progress_callback: Optional callback for progress updates.

    Returns:
        UploadSummary with results.

    Raises:
        FileNotFoundError: If any provided path does not exist.
        ValueError: If any provided path is not a file.
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`
            (raised by :meth:`GradualUploadRun.run`, after the local checks
            below).
    """
    pool = GradualClientPool()
    with pool.scope():
        start_time = time.time()
        file_list = [Path(p) for p in files]
        if not file_list:
            return UploadSummary(
                success=False,
                total=0,
                succeeded=0,
                failed=0,
                duration=0.0,
                errors=["No files provided"],
            )

        _validate_files_exist(file_list)

        dicom_file_list = [p for p in file_list if _is_dicom_like_path(p)]
        if not dicom_file_list:
            return UploadSummary(
                success=False,
                total=0,
                succeeded=0,
                failed=0,
                duration=0.0,
                errors=["No DICOM files found"],
            )

        _reject_duplicate_resolved_paths(dicom_file_list)

        display_root = _compute_display_root(dicom_file_list)

        run = GradualUploadRun(
            client=client,
            pool=pool,
            project=project,
            subject=subject,
            session=session,
            direct_archive=direct_archive,
            display_root=display_root,
            progress_callback=progress_callback,
            start_time=start_time,
        )
        return run.run(dicom_file_list, workers)
