"""A refreshed session token has to reach XNAT, not the dead session XNAT set.

XNAT answers a request on an expired session with 401 and a Set-Cookie for a
new anonymous JSESSIONID under its own path (``/xnat``). An httpx client keeps
that cookie, and from then on sends it ahead of any ``cookies=`` value; XNAT
reads the first one. So the retry after a session refresh still went out as the
dead session: an 85,140-file ``upload-exam`` whose file scan outlived XNAT's
15-minute session timeout got 401 on every warmup file after six successful
re-logins, and the circuit breaker aborted the whole upload.

The fake server below copies what XNAT did on prod: 401 plus Set-Cookie for
any request whose first JSESSIONID is not the live token.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from xnatctl.services.upload.gradual_client import (
    GradualClientPool,
    _upload_single_file_gradual,
)
from xnatctl.services.upload.rest_archive import upload_single_archive

BASE_URL = "https://xnat.example.org/xnat"
LIVE = "FRESH"


def _session_ids(request: httpx.Request) -> list[str]:
    cookie = request.headers.get("cookie", "")
    return [c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("JSESSIONID=")]


def _xnat(request: httpx.Request) -> httpx.Response:
    if _session_ids(request)[:1] == [LIVE]:
        return httpx.Response(200, text="/archive/projects/P/subjects/S/experiments/E")
    return httpx.Response(401, headers={"Set-Cookie": "JSESSIONID=ANON; Path=/xnat; HttpOnly"})


@contextmanager
def _fake_xnat(module: str) -> Iterator[list[httpx.Request]]:
    """Route every httpx.Client the module builds through the fake server."""
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _xnat(request)

    def factory(**kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(recording)
        return real_client(**kwargs)  # type: ignore[arg-type]

    with patch(f"{module}.httpx.Client", factory):
        yield seen


def _refresher() -> MagicMock:
    refresher = MagicMock()
    refresher.token = "STALE"
    refresher.refresh.return_value = LIVE
    return refresher


def _imports(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.url.path.endswith("/data/services/import")]


def _assert_retry_sent_only_the_live_token(seen: list[httpx.Request]) -> None:
    assert [_session_ids(r) for r in _imports(seen)] == [["STALE"], [LIVE]]


def test_gradual_upload_retries_with_the_refreshed_token(tmp_path: Path) -> None:
    dcm = tmp_path / "a.dcm"
    dcm.write_bytes(b"\x00" * 128 + b"DICM")
    pool = GradualClientPool()

    with _fake_xnat("xnatctl.services.upload.gradual_client") as seen, pool.scope():
        _name, ok, err = _upload_single_file_gradual(
            pool=pool,
            base_url=BASE_URL,
            session_refresher=_refresher(),
            verify_ssl=True,
            file_path=dcm,
            project="P",
            subject="S",
            session="E",
        )

    assert ok, f"the retry after the session refresh was refused: {err}"
    _assert_retry_sent_only_the_live_token(seen)


def test_batch_upload_retries_with_the_refreshed_token(tmp_path: Path) -> None:
    archive = tmp_path / "batch_1.zip"
    archive.write_bytes(b"zip-ish")

    with _fake_xnat("xnatctl.services.upload.rest_archive") as seen:
        result = upload_single_archive(
            xnat_client=MagicMock(server_version=None, base_url=BASE_URL),
            username="u",
            password="p",
            session_token="STALE",
            verify_ssl=True,
            timeout=30,
            archive_path=archive,
            project="P",
            subject="S",
            session="E",
            import_handler="DICOM-zip",
            ignore_unparsable=True,
            overwrite="delete",
            direct_archive=True,
            session_refresher=_refresher(),
        )

    assert result.success, f"the retry after the session refresh was refused: {result.error}"
    _assert_retry_sent_only_the_live_token(seen)
