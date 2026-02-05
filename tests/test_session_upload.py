from __future__ import annotations

import tarfile
import zipfile
from io import BytesIO

from xnatctl.cli.session import _do_single_upload, _safe_mtime, _zip_to_tar
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
        zip_to_tar=False,
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


def test_do_single_upload_converts_zip_to_tar(tmp_path) -> None:
    archive_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("alpha/first.dcm", b"file-1")
        zf.writestr("beta/second.dcm", b"file-2")

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self) -> None:
            self.headers = None
            self.body = None

        def post(self, path, params, data, headers, timeout):
            self.headers = headers
            self.body = data.read()
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
        zip_to_tar=True,
    )

    assert client.headers == {"Content-Type": "application/x-tar"}
    assert client.body is not None
    with tarfile.open(fileobj=BytesIO(client.body), mode="r") as tf:
        assert set(tf.getnames()) == {"alpha/first.dcm", "beta/second.dcm"}


# =============================================================================
# _safe_mtime Tests
# =============================================================================


def test_safe_mtime_valid_date() -> None:
    """Valid date tuple converts to timestamp."""
    date_time = (2024, 6, 15, 10, 30, 45)
    result = _safe_mtime(date_time)
    assert result > 0


def test_safe_mtime_invalid_date_returns_zero() -> None:
    """Invalid date (year=0) returns epoch timestamp."""
    date_time = (0, 0, 0, 0, 0, 0)
    result = _safe_mtime(date_time)
    assert result == 0.0


def test_safe_mtime_overflow_returns_zero() -> None:
    """Overflow date returns epoch timestamp."""
    date_time = (99999, 12, 31, 23, 59, 59)
    result = _safe_mtime(date_time)
    assert result == 0.0


# =============================================================================
# _zip_to_tar Tests
# =============================================================================


def test_zip_to_tar_converts_files(tmp_path) -> None:
    """ZIP file is converted to TAR with all files."""
    zip_path = tmp_path / "test.zip"
    tar_path = tmp_path / "test.tar"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("dir/file1.txt", b"content1")
        zf.writestr("file2.txt", b"content2")

    _zip_to_tar(zip_path, tar_path)

    assert tar_path.exists()
    with tarfile.open(tar_path, "r") as tf:
        names = tf.getnames()
        assert "dir/file1.txt" in names
        assert "file2.txt" in names


def test_zip_to_tar_preserves_directories(tmp_path) -> None:
    """Directories in ZIP are preserved in TAR."""
    zip_path = tmp_path / "test.zip"
    tar_path = tmp_path / "test.tar"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # Create a directory entry
        zf.writestr("mydir/", "")
        zf.writestr("mydir/file.txt", b"content")

    _zip_to_tar(zip_path, tar_path)

    with tarfile.open(tar_path, "r") as tf:
        members = {m.name: m for m in tf.getmembers()}
        assert "mydir" in members or "mydir/" in members


def test_zip_to_tar_raises_on_corrupted_zip(tmp_path) -> None:
    """Corrupted ZIP raises BadZipFile."""
    import pytest

    zip_path = tmp_path / "bad.zip"
    tar_path = tmp_path / "test.tar"

    # Write invalid ZIP data
    zip_path.write_bytes(b"not a valid zip file")

    with pytest.raises(zipfile.BadZipFile):
        _zip_to_tar(zip_path, tar_path)
