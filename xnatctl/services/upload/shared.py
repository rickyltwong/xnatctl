"""Cross-transport helpers shared by the upload transports.

Thread-safe session token refresh, DICOM file discovery, and batch-splitting
utilities used by more than one transport module.
"""

from __future__ import annotations

import logging
import ssl
import threading
from collections.abc import Sequence
from pathlib import Path

import httpx

from xnatctl.core.client import XNATClient
from xnatctl.core.timeouts import build_httpx_timeout

logger = logging.getLogger(__name__)

DICOM_EXTENSIONS = {".dcm", ".ima", ".img", ".dicom"}

# Shared across transports: the parallel REST batch and gradual-DICOM
# transports both default to 4 workers when the caller does not specify one.
DEFAULT_UPLOAD_WORKERS = 4


class SessionRefresher:
    """Thread-safe XNAT session token manager.

    When any worker thread encounters a 401 (expired session), it calls
    :meth:`refresh`.  Only the first thread to detect a stale token actually
    re-authenticates; concurrent callers wait on the lock and receive the
    already-refreshed token.

    A refresh here has to reach the *owning client* too. Workers hold their own
    copy of the token, so without ``owner`` the shared client kept the token it
    started with: after a multi-hour upload during which workers re-authenticated
    several times, the phases that run afterwards -- hierarchy resolution and
    resource attach in ``session upload-exam`` -- went out with a token that had
    been dead for hours, turning a completed upload into a failure at the last
    step.

    Args:
        base_url: XNAT server URL.
        verify_ssl: TLS verification: a bool, or an SSLContext carrying a
            profile's ``ca_bundle`` (``XNATClient.httpx_verify()``).
        token: Initial JSESSIONID token.
        username: Credentials for re-authentication.
        password: Credentials for re-authentication.
        owner: Client whose ``session_token`` should track refreshes.
    """

    def __init__(
        self,
        base_url: str,
        verify_ssl: bool | ssl.SSLContext,
        token: str | None,
        username: str | None,
        password: str | None,
        owner: XNATClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._verify_ssl = verify_ssl
        self._token = token
        self._username = username
        self._password = password
        self._owner = owner
        self._lock = threading.Lock()

    def _publish(self, token: str) -> None:
        """Push a fresh token to the owning client and the on-disk cache.

        Called with the lock held. Both writes are best-effort: a worker that
        just re-authenticated has a usable token in hand, and failing the
        upload because a *cache* could not be updated would be absurd.
        """
        if self._owner is not None:
            self._owner.session_token = token

        try:
            # Imported here rather than at module scope: this is the only place
            # the upload service touches the credential cache, and a top-level
            # import would tie every upload to the auth module's import cost.
            from xnatctl.core.auth import AuthManager

            if self._username:
                AuthManager().save_session(token, self._base_url, self._username)
        except Exception as e:
            logger.debug("Could not update the session cache after refresh: %s", e)

    @property
    def token(self) -> str | None:
        """Current session token (may be updated by any thread)."""
        with self._lock:
            return self._token

    def refresh(self, stale_token: str | None) -> str | None:
        """Re-authenticate and return a fresh token.

        Thread-safe: if another thread already refreshed past *stale_token*,
        the cached fresh token is returned without hitting the server again.

        Args:
            stale_token: The token that triggered the 401.

        Returns:
            Fresh session token, or the unchanged token if credentials are
            unavailable.
        """
        with self._lock:
            if self._token != stale_token:
                return self._token

            if not self._username or not self._password:
                logger.warning("Session expired but no credentials available for reauth")
                return self._token

            try:
                with httpx.Client(
                    base_url=self._base_url,
                    verify=self._verify_ssl,
                    timeout=build_httpx_timeout(30.0),  # connect fails fast
                ) as client:
                    resp = client.post(
                        "/data/JSESSION",
                        auth=(self._username, self._password),
                    )
                    if resp.status_code == 200 and "<html" not in resp.text.lower():
                        self._token = resp.text.strip()
                        self._publish(self._token)
                        logger.info("Session refreshed successfully")
                    else:
                        logger.error("Session refresh failed: HTTP %d", resp.status_code)
            except Exception:
                logger.exception("Session refresh failed")

            return self._token


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

        if path.is_symlink():
            try:
                resolved = path.resolve()
                if not resolved.exists():
                    continue
            except (OSError, ValueError):
                continue

        if _is_dicom_like_path(path, include_extensionless=include_extensionless):
            files.append(path)

    return sorted(files)


def _has_dicom_magic(path: Path) -> bool:
    """Return True if the file has the DICOM preamble magic bytes (DICM at offset 128)."""
    try:
        with open(path, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


def _is_dicom_like_path(path: Path, *, include_extensionless: bool = True) -> bool:
    """Return True when a path looks like a DICOM file we should ingest."""
    if path.name.startswith("."):
        return False

    suffix = path.suffix.lower()
    if suffix in DICOM_EXTENSIONS:
        return True
    if include_extensionless and suffix == "":
        return _has_dicom_magic(path)
    return False


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

    if current_batch:
        batches.append(current_batch)

    return batches


def split_into_n_batches(
    files: Sequence[Path],
    num_batches: int,
) -> list[list[Path]]:
    """Split files into N roughly equal batches using round-robin.

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


def _error_signature(error: str) -> str:
    """Collapse an error message to something comparable across files.

    Two rejections of the same *kind* differ in the file name and any UID the
    server echoes back, so comparing raw strings would never find them equal.
    The leading prefix is stable enough to group by and short enough not to
    reach the variable tail.
    """
    return error.strip()[:200]
