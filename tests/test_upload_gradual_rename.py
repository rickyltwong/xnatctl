"""Every gradual-DICOM file must ask XNAT to name it from its DICOM header.

A gradual upload posts each file as a raw request body, so XNAT names it
``upload.dcm``. Without ``rename=true``, XNAT's ``GradualDicomImporter`` keeps
that request name whenever the scan has no ``upload.dcm`` yet. When several of
a series' first files arrive together, they all become ``upload.dcm`` and
overwrite each other, and every POST still returns 200. A 102,598-file GE
session lost 21 files this way with 0 failures reported. With ``rename=true``
XNAT always uses its header-derived file name, which is unique per SOP
instance, so concurrent first files can no longer collide.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from xnatctl.services.upload.gradual_client import (
    GradualClientPool,
    _upload_single_file_gradual,
)


def test_gradual_upload_asks_xnat_to_rename_the_file(tmp_path: Path) -> None:
    dcm = tmp_path / "a.dcm"
    dcm.write_bytes(b"\x00" * 128 + b"DICM")
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def factory(**kwargs: object) -> httpx.Client:
        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="/prearchive/projects/P")

        kwargs["transport"] = httpx.MockTransport(record)
        return real_client(**kwargs)  # type: ignore[arg-type]

    refresher = MagicMock()
    refresher.token = "TOKEN"
    pool = GradualClientPool()

    with patch("xnatctl.services.upload.gradual_client.httpx.Client", factory), pool.scope():
        _name, ok, err = _upload_single_file_gradual(
            pool=pool,
            base_url="https://xnat.example.org/xnat",
            session_refresher=refresher,
            verify_ssl=True,
            file_path=dcm,
            project="P",
            subject="S",
            session="E",
        )

    assert ok, err
    (request,) = seen
    assert request.url.params["import-handler"] == "gradual-DICOM"
    assert request.url.params["rename"] == "true"
