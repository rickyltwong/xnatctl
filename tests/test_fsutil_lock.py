"""Tests for :func:`xnatctl.core.fsutil.exclusive_lock`.

The session cache holds this lock across each read-modify-write, so these
pin what it relies on: another process is kept out, a holder that dies leaves
no stale lock, and the lock file is owner-only.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from xnatctl.core.fsutil import POSIX_PERMISSIONS, exclusive_lock

# Holds the lock, says so, then waits to be killed.
_HOLDER = """
import sys, time
from pathlib import Path
from xnatctl.core.fsutil import exclusive_lock

with exclusive_lock(Path(sys.argv[1]), timeout=5):
    print("locked", flush=True)
    time.sleep(60)
"""


@pytest.fixture
def held_by_another_process(tmp_path: Path):
    """A child process holding the lock on ``tmp_path / "x.lock"``."""
    lock_path = tmp_path / "x.lock"
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        yield lock_path, child
    finally:
        child.kill()
        child.wait(timeout=10)


def test_another_process_holding_the_lock_keeps_this_one_out(held_by_another_process) -> None:
    lock_path, _child = held_by_another_process

    with pytest.raises(TimeoutError), exclusive_lock(lock_path, timeout=0.2):
        pass


def test_a_holder_that_dies_leaves_no_stale_lock(held_by_another_process) -> None:
    lock_path, child = held_by_another_process

    child.kill()
    child.wait(timeout=10)

    with exclusive_lock(lock_path, timeout=5):
        pass


def test_the_lock_is_free_again_after_the_block(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"

    with exclusive_lock(lock_path, timeout=0):
        pass

    with exclusive_lock(lock_path, timeout=0):
        pass


def test_threads_of_one_process_exclude_each_other(tmp_path: Path) -> None:
    """Each entry opens the file afresh, so a second thread waits too."""
    lock_path = tmp_path / "x.lock"
    inside = 0
    most_inside = 0
    counter_lock = threading.Lock()

    def worker() -> None:
        nonlocal inside, most_inside
        with exclusive_lock(lock_path, timeout=10):
            with counter_lock:
                inside += 1
                most_inside = max(most_inside, inside)
            threading.Event().wait(0.01)
            with counter_lock:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert most_inside == 1


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
def test_the_lock_file_is_owner_only(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"

    with exclusive_lock(lock_path, timeout=0):
        pass

    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
