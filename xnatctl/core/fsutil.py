"""Filesystem helpers for files that must stay readable only by their owner.

xnatctl writes two secrets under ``~/.config/xnatctl``: the cached JSESSIONID
(``.session``) and, until SEC-02 lands, plaintext profile passwords
(``config.yaml``). Creating either with a plain ``open(path, "w")`` applies the
process umask -- 0664 on a typical multi-user host -- so the secret is
world-readable for the whole window between creation and a follow-up ``chmod``,
and forever if that ``chmod`` fails. Two further gaps make the naive form worse
than it looks:

* the ``opener=`` mode only applies to files the call *creates*, so rewriting an
  existing 0644 file leaves it 0644;
* an in-place rewrite truncates first, so a concurrent reader can observe a
  half-written token.

:func:`atomic_private_write` closes all three by writing a fresh 0600 temp file
and ``os.replace``-ing it over the destination (SEC-08).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def _private_opener(path: str, flags: int) -> int:
    """``open()`` opener that creates files with :data:`PRIVATE_FILE_MODE`."""
    return os.open(path, flags, PRIVATE_FILE_MODE)


def open_private(path: Path) -> TextIO:
    """Open ``path`` for writing, creating it with 0600 permissions.

    Note the standard caveat: the mode applies only when this call creates the
    file. Prefer :func:`atomic_private_write` for anything that may already
    exist -- which is every secret xnatctl rewrites.
    """
    return open(path, "w", opener=_private_opener)


def open_private_append(path: Path) -> TextIO:
    """Open ``path`` for appending, creating it with 0600 permissions.

    The append-only counterpart of :func:`open_private`, for logs that must not
    be rewritten. Same caveat: the mode only applies when this call creates the
    file, so a caller that cares about a pre-existing file should follow up with
    :func:`restrict_permissions`.
    """
    return open(path, "a", opener=_private_opener)


def restrict_permissions(path: Path) -> bool:
    """``chmod`` ``path`` to 0600, warning on failure rather than passing.

    Returns True when the permissions were applied. A failure is reported, not
    swallowed: silently leaving a token world-readable is exactly the outcome
    SEC-08 exists to prevent.
    """
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError as e:
        logger.warning("Could not restrict permissions on %s: %s", path, e)
        return False
    return True


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` and any parents, restricted to the owner.

    ``mkdir(mode=0o700)`` is masked by the umask, so a freshly created directory
    can still land 0755; an explicit ``chmod`` follows. Only directories this
    call creates are chmod'ed -- an existing one may carry permissions the user
    set deliberately, and silently tightening it would be a surprise.
    """
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if existed:
        return
    try:
        os.chmod(path, PRIVATE_DIR_MODE)
    except OSError as e:
        # NOTE: os.chmod is largely a no-op on Windows, where the equivalent
        # protection is an ACL. Windows semantics are unaudited (GAP-10); warn
        # rather than fail, since an over-permissive directory is not worth
        # aborting a login over.
        logger.warning("Could not restrict permissions on %s: %s", path, e)


@contextmanager
def atomic_private_write(path: Path) -> Iterator[TextIO]:
    """Yield a writable handle whose contents replace ``path`` atomically.

    The temp file is created 0600 in the destination's own directory (so the
    replace stays within one filesystem) and ``os.replace`` hands the
    destination that inode -- and therefore that mode -- whatever the previous
    file's permissions were. Readers see either the old file or the new one,
    never a truncated one.

    On error the temp file is removed and ``path`` is left untouched.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open_private(tmp) as handle:
            yield handle
        os.replace(tmp, path)
    finally:
        # A successful replace consumed tmp; this only bites on the error path.
        tmp.unlink(missing_ok=True)
