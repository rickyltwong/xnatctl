from __future__ import annotations

import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.cli.session import _do_single_upload, _safe_mtime, _zip_to_tar
from xnatctl.core.cancellation import CancellationToken
from xnatctl.core.exceptions import PermissionDeniedError, SessionExpiredError, UploadError
from xnatctl.core.exceptions import TimeoutError as XNATTimeoutError
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.models.progress import UploadSummary
from xnatctl.services.import_service import archive_destination_params


class _FakeAuthClient:
    """Minimal authenticated client stub for CLI-glue tests."""

    is_authenticated = True

    def whoami(self) -> dict[str, str]:
        return {"username": "tester"}


class _FakeXNATClient:
    """Connection details only -- the fixed path never calls XNATClient.post.

    Deliberately has no ``post`` method: a regression back to posting through
    ``XNATClient`` (and its second retry ladder) fails these tests with an
    AttributeError instead of silently passing.
    """

    base_url = "https://xnat.example.org"
    username = "u"
    password = "p"
    session_token = "TOKEN"
    verify_ssl = True

    def httpx_verify(self) -> bool:
        return True


def _wire(monkeypatch, responses):
    """Capture the raw POSTs the services-layer uploader makes.

    ``_do_single_upload`` must reach XNAT through the services uploader -- one
    retry ladder, raw responses -- so these tests assert at the wire level.
    Returns (posts, clients): the recorded POST calls and the kwargs each
    ``httpx.Client`` was constructed with. The last response repeats once the
    list is consumed.
    """
    import xnatctl.services.uploads as uploads

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


def test_do_single_upload_sets_import_params(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, clients = _wire(monkeypatch, [httpx.Response(200)])

    _do_single_upload(
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
    assert call["headers"] == {"Content-Type": "application/zip"}
    assert call["cookies"] == {"JSESSIONID": "TOKEN"}
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


def test_do_single_upload_prearchive_sets_dest(tmp_path, monkeypatch) -> None:
    """--prearchive must send dest=/prearchive/projects/{project}, not
    Direct-Archive=false.

    Direct-Archive=false is defined by XNAT as "use standard upload
    mechanism", which on projects with auto-archive enabled still routes
    the session to the archive. Explicitly naming the prearchive dest is
    the documented way to force prearchive regardless of project config.
    """
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, _clients = _wire(monkeypatch, [httpx.Response(200)])

    _do_single_upload(
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


def test_do_single_upload_retries_a_transient_400(tmp_path, monkeypatch) -> None:
    """A transient import 400 must be retried, not fatal on first sight.

    The regression this pins: ``_do_single_upload`` used to wrap
    ``XNATClient.post`` -- whose ``_request`` raises a typed error on 400 --
    in ``upload_with_retry``, so the transient-vs-permanent 400 discrimination
    never saw a raw response and one concurrent-modification 400 killed the
    upload immediately.
    """
    monkeypatch.setattr(CancellationToken, "sleep", lambda self, seconds: False)
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, _clients = _wire(
        monkeypatch,
        [httpx.Response(400, text="Duplicate archive attempt"), httpx.Response(200)],
    )

    _do_single_upload(
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


def test_do_single_upload_permanent_400_fails_on_first_attempt(tmp_path, monkeypatch) -> None:
    """A permanent 400 (misconfigured upload) fails once, without retries."""
    archive_path = tmp_path / "sample.zip"
    archive_path.write_bytes(b"zip-data")
    posts, _clients = _wire(
        monkeypatch,
        [httpx.Response(400, text="Unable to identify destination project: PROJ")],
    )

    with pytest.raises(UploadError, match="Unable to identify destination project"):
        _do_single_upload(
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
    """Run _do_single_upload with fixed arguments; failures raise typed errors."""
    _do_single_upload(
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
    """Failures must raise the typed exceptions @handle_errors maps to exit codes.

    The old path let ``XNATClient.post`` raise these; the delegation to the
    services uploader returns strings, so without this mapping every failure
    collapsed to a general exit 1 -- a regression on a documented Stable
    surface (auth 3, network 4, permission 6).
    """

    def test_a_401_raises_session_expired(self, tmp_path, monkeypatch) -> None:
        archive_path = tmp_path / "sample.zip"
        archive_path.write_bytes(b"zip-data")
        # The refresher's reauth POST also lands on the fake client and gets
        # the same 401, so the refresh fails and the 401 stands.
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
        """A 5xx that survives every retry is a connection-class failure (exit 4)."""
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
        import xnatctl.services.uploads as uploads

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


def test_do_single_upload_converts_zip_to_tar(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("alpha/first.dcm", b"file-1")
        zf.writestr("beta/second.dcm", b"file-2")

    posts, _clients = _wire(monkeypatch, [httpx.Response(200)])

    _do_single_upload(
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
    assert call["headers"] == {"Content-Type": "application/x-tar"}
    assert call["body"] is not None
    with tarfile.open(fileobj=BytesIO(call["body"]), mode="r") as tf:
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
