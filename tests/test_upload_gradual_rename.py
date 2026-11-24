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
        return real_client(**kwargs)

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
