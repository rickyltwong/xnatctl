from __future__ import annotations

import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.cli.session import _do_single_upload, _safe_mtime, _zip_to_tar
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.models.progress import UploadSummary
from xnatctl.services.uploads import archive_destination_params


class _FakeAuthClient:
    """Minimal authenticated client stub for CLI-glue tests."""

    is_authenticated = True

    def whoami(self) -> dict[str, str]:
        return {"username": "tester"}


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
    assert "dest" not in params
    assert params["Ignore-Unparsable"] == "false"
    assert params["inbody"] == "true"
    assert params["overwrite_files"] == "true"
    assert params["quarantine"] == "false"
    assert params["triggerPipelines"] == "true"
    assert params["rename"] == "false"


def test_do_single_upload_prearchive_sets_dest(tmp_path) -> None:
    """--prearchive must send dest=/prearchive/projects/{project}, not
    Direct-Archive=false.

    Direct-Archive=false is defined by XNAT as "use standard upload
    mechanism", which on projects with auto-archive enabled still routes
    the session to the archive. Explicitly naming the prearchive dest is
    the documented way to force prearchive regardless of project config.
    """
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self) -> None:
            self.params = None

        def post(self, path, params, data, headers, timeout):
            self.params = params
            return FakeResponse()

    client = FakeClient()

    _do_single_upload(
        client,
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=False,
        ignore_unparsable=False,
        zip_to_tar=False,
    )

    params = client.params
    assert params is not None
    assert params["dest"] == "/prearchive/projects/PROJ"
    assert "Direct-Archive" not in params


def test_archive_destination_params_direct() -> None:
    """archive_destination_params(direct=True) returns only Direct-Archive."""
    assert archive_destination_params("PROJ", True) == {"Direct-Archive": "true"}


def test_archive_destination_params_prearchive() -> None:
    """archive_destination_params(direct=False) returns only dest, no Direct-Archive."""
    assert archive_destination_params("PROJ", False) == {"dest": "/prearchive/projects/PROJ"}


def test_session_upload_mode_gradual_forwards_prearchive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `session upload --mode gradual --prearchive` must
    forward direct_archive=False to the service.

    Before this was fixed, the CLI wrapper _upload_gradual_dicom() did
    not accept a direct_archive kwarg and did not pass it to
    service.upload_dicom_gradual(), so --prearchive was silently ignored
    on the gradual code path and uploads went direct-archive regardless.
    """
    captured: dict[str, object] = {}

    class UploadServiceSpy:
        def __init__(self, client: object) -> None:
            self.client = client

        def upload_dicom_gradual(
            self,
            *,
            source_path: Path,
            project: str,
            subject: str,
            session: str,
            workers: int,
            direct_archive: bool = True,
            progress_callback: object = None,
        ) -> UploadSummary:
            captured["direct_archive"] = direct_archive
            captured["project"] = project
            return UploadSummary(
                success=True, total=1, succeeded=1, failed=0, duration=0.0, errors=[]
            )

    client = _FakeAuthClient()

    def fake_get_client(self) -> _FakeAuthClient:
        return client

    monkeypatch.setattr("xnatctl.cli.common.Context.get_client", fake_get_client)
    monkeypatch.setattr("xnatctl.services.uploads.UploadService", UploadServiceSpy)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        monkeypatch.setenv("HOME", str(Path.cwd()))
        dicom_dir = Path("dicoms")
        dicom_dir.mkdir()
        (dicom_dir / "a.dcm").write_bytes(b"payload")

        result = runner.invoke(
            cli,
            [
                "session",
                "upload",
                str(dicom_dir),
                "-P",
                "PROJ",
                "-S",
                "SUBJ",
                "-E",
                "SESS",
                "--mode",
                "gradual",
                "--prearchive",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured.get("direct_archive") is False, (
        "--prearchive must propagate direct_archive=False through the "
        "gradual-mode CLI wrapper to the service"
    )


def test_session_upload_mode_gradual_default_is_direct_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --prearchive, gradual mode defaults to direct archive."""
    captured: dict[str, object] = {}

    class UploadServiceSpy:
        def __init__(self, client: object) -> None:
            self.client = client

        def upload_dicom_gradual(
            self,
            *,
            source_path: Path,
            project: str,
            subject: str,
            session: str,
            workers: int,
            direct_archive: bool = True,
            progress_callback: object = None,
        ) -> UploadSummary:
            captured["direct_archive"] = direct_archive
            return UploadSummary(
                success=True, total=1, succeeded=1, failed=0, duration=0.0, errors=[]
            )

    client = _FakeAuthClient()

    def fake_get_client(self) -> _FakeAuthClient:
        return client

    monkeypatch.setattr("xnatctl.cli.common.Context.get_client", fake_get_client)
    monkeypatch.setattr("xnatctl.services.uploads.UploadService", UploadServiceSpy)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        monkeypatch.setenv("HOME", str(Path.cwd()))
        dicom_dir = Path("dicoms")
        dicom_dir.mkdir()
        (dicom_dir / "a.dcm").write_bytes(b"payload")

        result = runner.invoke(
            cli,
            [
                "session",
                "upload",
                str(dicom_dir),
                "-P",
                "PROJ",
                "-S",
                "SUBJ",
                "-E",
                "SESS",
                "--mode",
                "gradual",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured.get("direct_archive") is True


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
