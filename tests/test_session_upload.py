from __future__ import annotations

from xnatctl.cli.session import _do_single_upload
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS


def test_do_single_upload_sets_import_params(tmp_path) -> None:
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self) -> None:
            self.path = None
            self.params = None
            self.headers = None
            self.timeout = None

        def post(self, path, params, data, headers, timeout):
            self.path = path
            self.params = params
            self.headers = headers
            self.timeout = timeout
            return FakeResponse()

    client = FakeClient()

    _do_single_upload(
        client,
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=True,
        ignore_unparsable=False,
    )

    assert client.path == "/data/services/import"
    assert client.timeout == DEFAULT_HTTP_TIMEOUT_SECONDS
    assert client.headers == {"Content-Type": "application/zip"}

    params = client.params
    assert params is not None
    assert params["import-handler"] == "DICOM-zip"
    assert params["project"] == "PROJ"
    assert params["subject"] == "SUBJ"
    assert params["session"] == "SESS"
    assert params["overwrite"] == "delete"
    assert params["Direct-Archive"] == "true"
    assert params["Ignore-Unparsable"] == "false"
    assert params["inbody"] == "true"
    assert params["overwrite_files"] == "true"
    assert params["quarantine"] == "false"
    assert params["triggerPipelines"] == "true"
    assert params["rename"] == "false"
