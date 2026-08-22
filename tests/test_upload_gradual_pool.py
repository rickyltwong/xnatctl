"""GradualClientPool: the property module-level globals made untestable.

The gradual-DICOM transport used to keep its thread-local httpx client, its
registry, and its scope refcount as module globals (now
``services/upload/gradual_client.py``).
That made two concurrent gradual operations share teardown: whichever one
finished first closed clients the other was still using. Moving the same
mechanism into a ``GradualClientPool`` instance -- created per operation and
passed down -- makes that failure mode structurally impossible, and testable:
two pool instances below never observe each other.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xnatctl.services.upload.gradual_client import GradualClientPool


class TestOneClientPerThread:
    def test_each_thread_gets_its_own_client(self) -> None:
        pool = GradualClientPool()
        clients: dict[int, object] = {}
        barrier = threading.Barrier(4)

        def worker(i: int) -> None:
            barrier.wait()
            clients[i] = pool.get_client(base_url="https://x", verify_ssl=True)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(clients) == 4
        assert len({id(c) for c in clients.values()}) == 4, "threads shared a client"

    def test_reentry_on_the_same_thread_reuses_the_client(self) -> None:
        pool = GradualClientPool()

        c1 = pool.get_client(base_url="https://x", verify_ssl=True)
        c2 = pool.get_client(base_url="https://x", verify_ssl=True)

        assert c1 is c2

    def test_a_different_base_url_gets_a_new_client(self) -> None:
        """The client is keyed on (base_url, verify_ssl), not just the thread."""
        pool = GradualClientPool()

        c1 = pool.get_client(base_url="https://x", verify_ssl=True)
        c2 = pool.get_client(base_url="https://y", verify_ssl=True)

        assert c1 is not c2


class TestTeardownAtRefcountZero:
    def test_scope_closes_the_client_on_exit(self) -> None:
        pool = GradualClientPool()

        with pool.scope():
            client = pool.get_client(base_url="https://x", verify_ssl=True)
            assert not client.is_closed

        assert client.is_closed

    def test_nested_scopes_only_close_when_the_outermost_exits(self) -> None:
        """Refcounted: an inner scope exiting must not tear down a client an
        outer, still-active scope is relying on.
        """
        pool = GradualClientPool()

        with pool.scope():
            client = pool.get_client(base_url="https://x", verify_ssl=True)
            with pool.scope():
                pass
            assert not client.is_closed, "inner scope exit tore down a shared client"

        assert client.is_closed

    def test_get_client_after_teardown_creates_a_fresh_one(self) -> None:
        pool = GradualClientPool()

        with pool.scope():
            c1 = pool.get_client(base_url="https://x", verify_ssl=True)

        assert c1.is_closed

        with pool.scope():
            c2 = pool.get_client(base_url="https://x", verify_ssl=True)
            assert c2 is not c1
            assert not c2.is_closed


class TestPoolsAreIndependent:
    def test_closing_one_pool_does_not_touch_another(self) -> None:
        """The bug the module globals had: two concurrent gradual uploads
        would close each other's HTTP clients out from under them.
        """
        pool_a = GradualClientPool()
        pool_b = GradualClientPool()

        with pool_a.scope():
            client_a = pool_a.get_client(base_url="https://x", verify_ssl=True)

            with pool_b.scope():
                client_b = pool_b.get_client(base_url="https://x", verify_ssl=True)

            # pool_b's scope exited and closed pool_b's client; pool_a's must
            # be untouched.
            assert client_b.is_closed
            assert not client_a.is_closed

        assert client_a.is_closed

    def test_two_pools_never_share_a_client(self) -> None:
        pool_a = GradualClientPool()
        pool_b = GradualClientPool()

        client_a = pool_a.get_client(base_url="https://x", verify_ssl=True)
        client_b = pool_b.get_client(base_url="https://x", verify_ssl=True)

        assert client_a is not client_b


class TestDirectRunEntersItsOwnScope:
    """GradualUploadRun.run() is public and reachable without going through
    UploadService, which always supplies an outer ``pool.scope()``. Without a
    scope of its own, a directly-constructed run would leave its pool's real
    HTTP clients open after ``run()`` returns.
    """

    def test_run_without_an_outer_scope_still_closes_the_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.services.upload.gradual_client as gradual_client_mod
        from xnatctl.services.upload.gradual import GradualUploadRun

        # Real _upload_single_file_gradual runs (so it really calls
        # pool.get_client), but the HTTP retry ladder is faked so no network
        # call happens.
        monkeypatch.setattr(
            gradual_client_mod,
            "upload_with_retry",
            lambda fn, **kwargs: MagicMock(status_code=200),
        )

        dcm = tmp_path / "a.dcm"
        dcm.write_bytes(b"\x00" * 128 + b"DICM")

        client = MagicMock()
        client.base_url = "https://xnat.example.org"
        client.session_token = "TOK"
        client.username = "u"
        client.password = "p"

        pool = GradualClientPool()
        created_clients: list[object] = []
        real_get_client = pool.get_client

        def spy_get_client(**kwargs: object) -> object:
            c = real_get_client(**kwargs)  # type: ignore[arg-type]
            created_clients.append(c)
            return c

        monkeypatch.setattr(pool, "get_client", spy_get_client)

        run = GradualUploadRun(
            client=client,
            pool=pool,
            project="P",
            subject="S",
            session="E",
            direct_archive=True,
            display_root=tmp_path,
            progress_callback=None,
            start_time=0.0,
        )

        summary = run.run([dcm], workers=1)

        assert summary.success is True
        assert created_clients, "the pool never created a client -- this test proves nothing"
        assert all(c.is_closed for c in created_clients), (
            "run() without an outer scope left the pool's HTTP client open"
        )
