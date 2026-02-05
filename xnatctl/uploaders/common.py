"""Common utilities for uploader modules."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# File extensions recognized as DICOM
DICOM_EXTENSIONS = {".dcm", ".ima", ".img", ".dicom"}

# Retry defaults for upload operations
UPLOAD_MAX_RETRIES = 5
UPLOAD_RETRY_BACKOFF_BASE = 2  # seconds: 2, 4, 8, 16, 32
RETRYABLE_STATUS_CODES = {400, 429, 500, 502, 503, 504}


def collect_dicom_files(
    root: Path,
    *,
    include_extensionless: bool = True,
) -> list[Path]:
    """Recursively collect DICOM-like files under a root directory.

    Args:
        root: Root directory to search.
        include_extensionless: If True, include files without extensions
            (common for raw DICOM from scanners).

    Returns:
        Sorted list of file paths.

    Raises:
        ValueError: If root is not a directory.
    """
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue

        # Skip hidden files
        if path.name.startswith("."):
            continue

        # Skip broken symlinks or symlinks pointing outside root
        if path.is_symlink():
            try:
                resolved = path.resolve()
                if not resolved.exists():
                    continue
            except (OSError, ValueError):
                continue

        suffix = path.suffix.lower()
        if suffix in DICOM_EXTENSIONS:
            files.append(path)
        elif include_extensionless and suffix == "":
            files.append(path)

    return sorted(files)


def split_into_batches(
    files: Sequence[Path],
    batch_size: int,
) -> list[list[Path]]:
    """Split files into batches of specified size.

    Args:
        files: Sequence of file paths to split.
        batch_size: Maximum files per batch.

    Returns:
        List of batches, each batch being a list of paths.
    """
    if not files:
        return []

    if batch_size <= 0:
        return [list(files)]

    batches: list[list[Path]] = []
    current_batch: list[Path] = []

    for file_path in files:
        current_batch.append(file_path)
        if len(current_batch) >= batch_size:
            batches.append(current_batch)
            current_batch = []

    # Add remaining files as final batch
    if current_batch:
        batches.append(current_batch)

    return batches


def split_into_n_batches(
    files: Sequence[Path],
    num_batches: int,
) -> list[list[Path]]:
    """Split files into N roughly equal batches using round-robin.

    This is useful when you want a fixed number of parallel workers
    regardless of file count.

    Args:
        files: Sequence of file paths to split.
        num_batches: Number of batches to create.

    Returns:
        List of batches, each batch being a list of paths.
    """
    if not files:
        return []

    if num_batches <= 0:
        return [list(files)]

    actual_batches = min(num_batches, len(files))
    batches: list[list[Path]] = [[] for _ in range(actual_batches)]

    for idx, file_path in enumerate(files):
        batches[idx % actual_batches].append(file_path)

    return batches


def is_retryable_status(status_code: int) -> bool:
    """Check if an HTTP status code warrants a retry.

    Retryable: 429 (rate limit), 5xx (server errors).
    Non-retryable: 2xx (success), 401/403 (auth), other 4xx (client error).
    """
    return status_code in RETRYABLE_STATUS_CODES


def upload_with_retry(
    upload_fn: Callable[[], Any],
    *,
    max_retries: int = UPLOAD_MAX_RETRIES,
    backoff_base: int = UPLOAD_RETRY_BACKOFF_BASE,
    label: str = "upload",
) -> Any:
    """Execute an upload function with retry on transient HTTP errors.

    Args:
        upload_fn: Callable that performs the upload and returns an httpx.Response.
                   Will be called multiple times on retry - must be idempotent.
        max_retries: Maximum number of retries (default: 3).
        backoff_base: Base for exponential backoff in seconds (default: 2).
        label: Label for log messages.

    Returns:
        The httpx.Response from a successful attempt.

    Raises:
        The last exception if all retries are exhausted and no response was obtained.
    """
    import httpx

    last_resp = None
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = upload_fn()
            # Success or non-retryable error - return immediately
            if not is_retryable_status(resp.status_code):
                return resp
            # Retryable status - log and retry
            last_resp = resp
            last_exc = None
            if attempt < max_retries:
                delay = backoff_base ** (attempt + 1)
                logger.warning(
                    "%s: HTTP %d on attempt %d/%d, retrying in %ds",
                    label,
                    resp.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            last_resp = None
            if attempt < max_retries:
                delay = backoff_base ** (attempt + 1)
                logger.warning(
                    "%s: %s on attempt %d/%d, retrying in %ds",
                    label,
                    type(e).__name__,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)

    # All retries exhausted
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label}: all retries exhausted with no response")
