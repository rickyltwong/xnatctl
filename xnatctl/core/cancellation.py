"""Cooperative cancellation for parallel operations.

Without this, Ctrl+C during a parallel upload is nearly inert: every batch is
submitted to a :class:`~concurrent.futures.ThreadPoolExecutor` up front, so an
interrupt raised in the main thread (inside the ``as_completed`` loop) unwinds
into ``ThreadPoolExecutor.__exit__``, which calls ``shutdown(wait=True)``. That
waits for *every queued batch to run to completion* -- the queue is never
cancelled -- so a 500-file upload keeps uploading after the user asks it to
stop, silently, for as long as the transfer has left to run.

Two mechanisms prevent that, and both are needed:

* ``shutdown(wait=False, cancel_futures=True)`` drops work that has not started.
  This is the bulk of it, but it cannot touch a batch already in a worker.
* A shared flag the workers themselves poll. An in-flight HTTP request cannot be
  interrupted, but the code *around* it can stop early -- above all the upload
  retry ladder, which otherwise sleeps 2+4+8+16+32s per failing batch while the
  user waits for a shutdown they already requested.

Living in ``core/`` rather than ``cli/`` is deliberate: the executor loops this
serves are in ``services/``, and a later refactor that moves command bodies
around must not have to rediscover this.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


class CancellationToken:
    """A flag the main thread sets and worker threads poll.

    Cooperative by design: setting it asks workers to stop at their next check
    point. Nothing is forcibly killed, so a worker mid-request finishes that
    request (bounded by the HTTP timeouts) rather than leaving a half-written
    archive or a leaked connection behind.

    Safe to share across threads -- it wraps a :class:`threading.Event`.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Idempotent."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._event.is_set()

    def sleep(self, seconds: float) -> bool:
        """Sleep up to ``seconds``, waking immediately if cancelled.

        The drop-in replacement for ``time.sleep`` in any retry backoff that
        runs inside a worker. Returns True when the wait ended because
        cancellation was requested, which callers use to abandon the retry
        rather than press on.
        """
        return self._event.wait(seconds)


#: A token that is never cancelled, for call sites that do not opt in.
#:
#: Lets worker functions take ``cancel_token`` unconditionally instead of
#: threading ``None`` checks through every loop.
NULL_TOKEN = CancellationToken()


@contextmanager
def cancellable_pool(
    max_workers: int, token: CancellationToken | None = None
) -> Iterator[tuple[ThreadPoolExecutor, CancellationToken]]:
    """Yield a thread pool that a Ctrl+C actually stops.

    Replaces ``with ThreadPoolExecutor(max_workers=n) as executor:``. On any
    exception escaping the body -- ``KeyboardInterrupt`` above all -- the token
    is cancelled and the queue is dropped *before* the pool is joined, so
    shutdown waits only for work already running instead of for the entire
    backlog.

    The final ``shutdown(wait=True)`` is deliberate, not an oversight: callers
    delete temp archives in their own ``finally`` blocks, and returning while a
    worker still has one open would trade a slow exit for a corrupt one.

    Args:
        max_workers: Thread count, as for :class:`ThreadPoolExecutor`.
        token: An existing token to reuse, e.g. one shared with a second pool.
            A fresh token is created when omitted.

    Yields:
        The executor and the token to hand to worker functions.
    """
    token = token or CancellationToken()
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        yield executor, token
    except BaseException:
        token.cancel()
        # Before the join below: this is what drops the not-yet-started
        # backlog. Reversing the order would wait for all of it first.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=True)
