"""Centralized timeout defaults for xnatctl."""

from __future__ import annotations

import httpx

# Read/write ceiling. Deliberately generous: a single large DICOM transfer can
# legitimately stream for hours, so the read phase must not time out.
DEFAULT_HTTP_TIMEOUT_SECONDS = 21600

# Connect timeout. A blackholed (firewall-DROPped) host must fail in seconds, not
# hours -- the connect phase has nothing to do with transfer size.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10

# Pool-acquire timeout: bounded so a saturated connection pool surfaces quickly
# instead of inheriting the multi-hour read ceiling.
DEFAULT_POOL_TIMEOUT_SECONDS = 30

# Max seconds `session upload-exam` waits for DICOM to finish archiving before
# attaching session resources. Kept generous: large sessions (100k+ files) can
# take well over an hour to archive, and a premature timeout aborts the command
# before resources/misc files are attached.
DEFAULT_ARCHIVE_WAIT_SECONDS = 14400  # 4 hours


def build_httpx_timeout(read_timeout: float | None) -> httpx.Timeout:
    """Build a structured httpx.Timeout with a fast connect and a long read.

     Passing a bare scalar to httpx sets connect/read/write/pool all to that value,
     which is why a 6-hour read ceiling used to also govern connect and let a
     blackholed host hang any command for hours. This keeps the connect (and pool)
     phases short while letting the read/write phases run as long as ``read_timeout``
     allows. Centralized here so every httpx.Client in the package shares one policy
    .

    Args:
         read_timeout: Read/write ceiling in seconds; ``None`` falls back to the
             6-hour default.

    Returns:
         An ``httpx.Timeout`` with ``connect`` fixed at
         :data:`DEFAULT_CONNECT_TIMEOUT_SECONDS`.
    """
    read = float(read_timeout if read_timeout is not None else DEFAULT_HTTP_TIMEOUT_SECONDS)
    return httpx.Timeout(
        connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read=read,
        write=read,
        pool=DEFAULT_POOL_TIMEOUT_SECONDS,
    )
