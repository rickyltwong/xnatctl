"""Per-operation HTTP client pool and single-file transport for gradual-DICOM.

Split out of :mod:`gradual` to keep that module under the line budget --
``GradualClientPool`` and :func:`_upload_single_file_gradual` are the leaf
building block the run orchestration in :mod:`gradual` is built on.
"""

from __future__ import annotations

import contextlib
import logging
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken
from xnatctl.core.retry import upload_with_retry
from xnatctl.core.timeouts import build_httpx_timeout
from xnatctl.services.import_service import IMPORT_ENDPOINT, build_import_params

from .shared import SessionRefresher, session_cookie_header

logger = logging.getLogger(__name__)

_GRADUAL_HTTP_TIMEOUT_SECONDS = 120.0


class GradualClientPool:
    """Per-operation pool of thread-local keep-alive httpx clients.

    Gradual uploads use a per-thread httpx.Client. Pool state is instance-level
    -- created fresh per upload operation and passed down -- so that two
    concurrent gradual operations never share (and cannot close) each other's
    clients, which a module-level registry could not guarantee.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._registry: list[httpx.Client] = []
        self._scope_lock = threading.Lock()
        self._scope_refcount = 0

    def get_client(self, *, base_url: str, verify_ssl: bool | ssl.SSLContext) -> httpx.Client:
        """Get (or create) this thread's httpx.Client for gradual-DICOM uploads."""
        key = (base_url, verify_ssl)
        client: httpx.Client | None = getattr(self._local, "client", None)
        client_key: tuple[str, bool | ssl.SSLContext] | None = getattr(self._local, "key", None)

        if client is None or client_key != key or client.is_closed:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001  # best-effort cleanup: closing a stale httpx client must not fail the operation
                    pass

            client = httpx.Client(
                base_url=base_url,
                timeout=build_httpx_timeout(_GRADUAL_HTTP_TIMEOUT_SECONDS),
                verify=verify_ssl,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
            self._local.client = client
            self._local.key = key
            with self._registry_lock:
                self._registry.append(client)

        return client

    def close_all(self) -> None:
        """Close every thread-local client this pool has created."""
        try:
            self._local.client = None
            self._local.key = None
        except Exception:  # noqa: BLE001  # best-effort cleanup: clearing thread-local client refs
            pass

        with self._registry_lock:
            clients = list(self._registry)
            self._registry.clear()
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001  # best-effort cleanup: closing pooled httpx clients must not fail teardown
                pass

    @contextlib.contextmanager
    def scope(self) -> Iterator[None]:
        """Scope this pool's client lifecycle to an upload operation.

        Refcounts nested/re-entrant uses of the same pool instance and only
        closes clients when the outermost scope exits.
        """
        with self._scope_lock:
            self._scope_refcount += 1

        try:
            yield
        finally:
            with self._scope_lock:
                self._scope_refcount -= 1
                if self._scope_refcount <= 0:
                    self._scope_refcount = 0
                    self.close_all()


def _upload_single_file_gradual(
    *,
    pool: GradualClientPool,
    base_url: str,
    session_refresher: SessionRefresher,
    verify_ssl: bool | ssl.SSLContext,
    file_path: Path,
    display_path: str | None = None,
    project: str,
    subject: str,
    session: str,
    direct_archive: bool = True,
    cancel_token: CancellationToken = NULL_TOKEN,
) -> tuple[str, bool, str]:
    name = display_path or file_path.name

    try:
        client = pool.get_client(base_url=base_url, verify_ssl=verify_ssl)

        def _do_upload(token: str | None) -> httpx.Response:
            headers = {"Content-Type": "application/dicom", **session_cookie_header(token)}

            def _attempt() -> httpx.Response:
                with open(file_path, "rb") as f:
                    return client.post(
                        IMPORT_ENDPOINT,
                        params=build_import_params(
                            import_handler="gradual-DICOM",
                            project=project,
                            subject=subject,
                            session=session,
                            entity_keys="experiment",
                            rename=True,
                            inbody=True,
                            direct_archive=direct_archive,
                        ),
                        content=f,
                        headers=headers,
                    )

            return upload_with_retry(
                _attempt, label=f"gradual-DICOM {name}", cancel_token=cancel_token
            )

        token = session_refresher.token
        resp = _do_upload(token)

        if resp.status_code == 401:
            new_token = session_refresher.refresh(token)
            if new_token != token:
                resp = _do_upload(new_token)
                if resp.status_code == 401:
                    logger.warning("Still 401 after session refresh for %s", name)
            else:
                logger.debug("Session refresh returned same token for %s", name)

        if 200 <= resp.status_code < 300:
            return name, True, ""

        return name, False, _extract_error_detail(resp)
    except Exception as e:  # noqa: BLE001  # per-file worker-pool isolation: any failure becomes a documented (name, False, msg) tuple
        return name, False, str(e)


def _extract_error_detail(resp: httpx.Response) -> str:
    snippet = ""
    try:
        snippet = resp.text.strip().replace("\n", " ")
    except Exception:  # noqa: BLE001  # best-effort debug-info extraction: must not crash the error-reporting path itself
        snippet = ""
    if snippet:
        snippet = snippet[:200]

    detail = f"HTTP {resp.status_code}"
    if snippet:
        detail = f"{detail}: {snippet}"
    return detail
