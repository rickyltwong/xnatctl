"""Atomic file-streaming primitive shared by every download path.

Owns ``stream_to_file`` -- the retry-aware GET-to-disk streamer every method
in this package writes through -- and nothing else.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import DownloadError

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class StreamedFile(NamedTuple):
    """Result of :func:`stream_to_file`."""

    bytes_written: int
    """Bytes written to the destination."""

    content_length: int | None
    """The response Content-Length, or None when the server did not send one."""


def _declared_content_length(response: httpx.Response) -> int | None:
    """The Content-Length usable for byte-count verification, or None.

    None when the header is absent, malformed, or negative -- and when a
    non-identity Content-Encoding means httpx's decoded byte count would not
    match the wire length anyway.
    """
    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
        return None
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError:
        return None
    return length if length >= 0 else None


def stream_to_file(
    client: XNATClient,
    path: str,
    dest: Path,
    *,
    params: dict[str, Any] | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    chunk_size: int = _DOWNLOAD_CHUNK_SIZE,
) -> StreamedFile:
    """Stream a GET to ``dest`` atomically, through the client's retry/auth path.

    Writes to a sibling ``.part`` file and renames on success, so a network
    drop never leaves a truncated file that looks complete. If the response
    carries a nonzero Content-Length that disagrees with the bytes written, the
    download is rejected. On any failure the ``.part`` is removed and no
    ``dest`` is produced.

    Args:
        client: Client to stream through (retry ladder, typed errors, auth).
        path: API path.
        dest: Final destination path.
        params: Query parameters.
        progress_cb: Called after each chunk with
            ``(bytes_written, content_length)``.
        chunk_size: Read chunk size in bytes.

    Returns:
        The bytes written and the response Content-Length.

    Raises:
        DownloadError: On a Content-Length mismatch.
    """
    # Unique per process and thread, so parallel workers (or two commands)
    # aiming at the same destination cannot truncate or unlink each other's
    # in-flight temporary.
    part = dest.with_name(f"{dest.name}.{os.getpid()}-{threading.get_ident()}.part")
    bytes_written = 0
    content_length: int | None = None
    try:
        with client.stream("GET", path, params=params) as response:
            content_length = _declared_content_length(response)

            with open(part, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if progress_cb is not None:
                        progress_cb(bytes_written, content_length)

        if content_length is not None and content_length != 0 and bytes_written != content_length:
            raise DownloadError(
                f"Incomplete download of {path}: wrote {bytes_written} bytes but the "
                f"server declared Content-Length {content_length}",
                path,
            )

        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    return StreamedFile(bytes_written, content_length)
