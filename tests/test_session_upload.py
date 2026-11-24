from __future__ import annotations

import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.cancellation import CancellationToken
from xnatctl.core.exceptions import PermissionDeniedError, SessionExpiredError, UploadError
from xnatctl.core.exceptions import RequestTimeoutError as XNATTimeoutError
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.models.progress import UploadSummary
from xnatctl.services.import_service import archive_destination_params
from xnatctl.services.upload.archives import _safe_mtime, _zip_to_tar
from xnatctl.services.upload.rest_archive import upload_archive_or_raise


class _FakeAuthClient:
    is_authenticated = True

    def whoami(self) -> dict[str, str]:
        return {"username": "tester"}


class _FakeXNATClient:
    base_url = "https://xnat.example.org"
    username = "u"
    password = "p"
    session_token = "TOKEN"
    verify_ssl = True
    server_version = None

    def httpx_verify(self) -> bool:
        return True


def _wire(monkeypatch, responses):
    import xnatctl.services.upload.rest_archive as uploads

    posts: list[dict] = []
    clients: list[dict] = []
    queue = list(responses)

    class FakeHTTPClient:
        def __init__(self, *a, **kw) -> None:
            clients.append(kw)

        def __enter__(self) -> FakeHTTPClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, **kw) -> httpx.Response:
            body = kw.get("content")
            posts.append(
                {
                    "url": url,
                    "params": kw.get("params"),
                    "headers": kw.get("headers"),
                    "cookies": kw.get("cookies"),
                    "body": body.read() if body is not None else None,
                }
            )
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def delete(self, *a, **kw) -> None:
            return None

    monkeypatch.setattr(uploads.httpx, "Client", FakeHTTPClient)
    return posts, clients


def test_upload_archive_or_raise_sets_import_params(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, clients = _wire(monkeypatch, [httpx.Response(200)])

    upload_archive_or_raise(
        _FakeXNATClient(),
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=True,
        ignore_unparsable=False,
        zip_to_tar=False,
    )

    (call,) = posts
    assert call["url"] == "/data/services/import"
    assert call["headers"] == {"Content-Type": "application/zip", "Cookie": "JSESSIONID=TOKEN"}
    assert call["cookies"] is None
    assert call["body"] == b"zip-data"
    assert clients[0]["timeout"].read == DEFAULT_HTTP_TIMEOUT_SECONDS

    params = call["params"]
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


def test_upload_archive_or_raise_prearchive_sets_dest(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, _clients = _wire(monkeypatch, [httpx.Response(200)])

    upload_archive_or_raise(
        _FakeXNATClient(),
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=False,
        ignore_unparsable=False,
        zip_to_tar=False,
    )

    (call,) = posts
    params = call["params"]
    assert params["dest"] == "/prearchive/projects/PROJ"
    assert "Direct-Archive" not in params


def test_upload_archive_or_raise_retries_a_transient_400(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(CancellationToken, "sleep", lambda self, seconds: False)
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, _clients = _wire(
        monkeypatch,
        [httpx.Response(400, text="Duplicate archive attempt"), httpx.Response(200)],
    )

    upload_archive_or_raise(
        _FakeXNATClient(),
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=True,
        ignore_unparsable=False,
        zip_to_tar=False,
    )

    assert len(posts) == 2, "the transient 400 was not retried"


def test_upload_archive_or_raise_permanent_400_fails_on_first_attempt(
    tmp_path, monkeypatch
) -> None:
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, _clients = _wire(
        monkeypatch,
        [httpx.Response(400, text="Unable to identify destination project: PROJ")],
    )

    with pytest.raises(UploadError, match="Unable to identify destination project"):
        upload_archive_or_raise(
            _FakeXNATClient(),
            archive_path,
            project="PROJ",
            subject="SUBJ",
            session="SESS",
            overwrite="delete",
            direct_archive=True,
            ignore_unparsable=False,
            zip_to_tar=False,
        )

    assert len(posts) == 1, "a permanent 400 burned retries"


def _upload_one(archive_path):
    upload_archive_or_raise(
        _FakeXNATClient(),
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=True,
        ignore_unparsable=False,
        zip_to_tar=False,
    )


class TestSingleUploadKeepsTheExitCodeTaxonomy:
    def test_a_401_raises_session_expired(self, tmp_path, monkeypatch) -> None:
        archive_path = tmp_path / "sample.zip"
        archive_path.write_bytes(b"zip-data")
        _wire(monkeypatch, [httpx.Response(401, text="<html>login</html>")])

        with pytest.raises(SessionExpiredError):
            _upload_one(archive_path)

    def test_a_403_raises_permission_denied(self, tmp_path, monkeypatch) -> None:
        archive_path = tmp_path / "sample.zip"
        archive_path.write_bytes(b"zip-data")
        _wire(monkeypatch, [httpx.Response(403, text="<html>forbidden</html>")])

        with pytest.raises(PermissionDeniedError):
            _upload_one(archive_path)

    def test_exhausted_503_retries_raise_retry_exhausted(self, tmp_path, monkeypatch) -> None:
        from xnatctl.core.exceptions import RetryExhaustedError

        monkeypatch.setattr(CancellationToken, "sleep", lambda self, seconds: False)
        archive_path = tmp_path / "sample.zip"
        archive_path.write_bytes(b"zip-data")
        posts, _clients = _wire(monkeypatch, [httpx.Response(503, text="down for maintenance")])

        with pytest.raises(RetryExhaustedError):
            _upload_one(archive_path)

        assert len(posts) > 1, "the 503 was not retried before giving up"

    def test_a_connect_timeout_raises_timeout(self, tmp_path, monkeypatch) -> None:
        archive_path = tmp_path / "sample.zip"
        archive_path.write_bytes(b"zip-data")
        import xnatctl.services.upload.rest_archive as uploads

        class TimingOutClient:
            def __init__(self, *a, **kw) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a) -> None:
                return None

            def post(self, url, **kw):
                raise httpx.ConnectTimeout("connect phase timed out")

            def delete(self, *a, **kw) -> None:
                return None

        monkeypatch.setattr(uploads.httpx, "Client", TimingOutClient)

        with pytest.raises(XNATTimeoutError):
            _upload_one(archive_path)


def test_archive_destination_params_direct() -> None:
    assert archive_destination_params("PROJ", True) == {"Direct-Archive": "true"}


def test_archive_destination_params_prearchive() -> None:
    assert archive_destination_params("PROJ", False) == {"dest": "/prearchive/projects/PROJ"}


def test_session_upload_mode_gradual_forwards_prearchive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr("xnatctl.services.upload.UploadService", UploadServiceSpy)

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
    monkeypatch.setattr("xnatctl.services.upload.UploadService", UploadServiceSpy)

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


def test_upload_archive_or_raise_converts_zip_to_tar(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("alpha/first.dcm", b"file-1")
        zf.writestr("beta/second.dcm", b"file-2")

    posts, _clients = _wire(monkeypatch, [httpx.Response(200)])

    upload_archive_or_raise(
        _FakeXNATClient(),
        archive_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        overwrite="delete",
        direct_archive=True,
        ignore_unparsable=False,
        zip_to_tar=True,
    )

    (call,) = posts
    assert call["headers"] == {"Content-Type": "application/x-tar", "Cookie": "JSESSIONID=TOKEN"}
    assert call["body"] is not None
    with tarfile.open(fileobj=BytesIO(call["body"]), mode="r") as tf:
        assert set(tf.getnames()) == {"alpha/first.dcm", "beta/second.dcm"}


def test_safe_mtime_valid_date() -> None:
    date_time = (2024, 6, 15, 10, 30, 45)
    result = _safe_mtime(date_time)
    assert result > 0


def test_safe_mtime_invalid_date_returns_zero() -> None:
    date_time = (0, 0, 0, 0, 0, 0)
    result = _safe_mtime(date_time)
    assert result == 0.0


def test_safe_mtime_overflow_returns_zero() -> None:
    date_time = (99999, 12, 31, 23, 59, 59)
    result = _safe_mtime(date_time)
    assert result == 0.0


def test_zip_to_tar_converts_files(tmp_path) -> None:
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
    zip_path = tmp_path / "test.zip"
    tar_path = tmp_path / "test.tar"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("mydir/", "")
        zf.writestr("mydir/file.txt", b"content")

    _zip_to_tar(zip_path, tar_path)

    with tarfile.open(tar_path, "r") as tf:
        members = {m.name: m for m in tf.getmembers()}
        assert "mydir" in members or "mydir/" in members


def test_zip_to_tar_raises_on_corrupted_zip(tmp_path) -> None:
    import pytest

    zip_path = tmp_path / "bad.zip"
    tar_path = tmp_path / "test.tar"

    zip_path.write_bytes(b"not a valid zip file")

    with pytest.raises(zipfile.BadZipFile):
        _zip_to_tar(zip_path, tar_path)
