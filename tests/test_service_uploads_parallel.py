"""Coverage for the parallel batched-archive REST upload transport.

`upload_dicom_parallel` is the default `session upload` path for a directory,
and it had no test at all: batch splitting, the import parameters actually put
on the wire, the per-thread authentication dance, partial-failure aggregation,
and archive cleanup were all unverified.

Requests run through `httpx.MockTransport`, so assertions are about real
`httpx.Request` objects -- the params below are what XNAT would receive.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.models.progress import OperationPhase, UploadProgress
from xnatctl.services.uploads import UploadService

Handler = Callable[[httpx.Request], httpx.Response]


@contextmanager
def mock_uploads_http(handler: Handler) -> Iterator[list[httpx.Request]]:
    """Route the transport's own httpx.Client through a MockTransport.

    `upload_single_archive` constructs its client inside the worker thread
    (that is the thread-safety design), so there is no injection point on the
    service. Wrapping the constructor is the least invasive seam that still
    exercises real request building.
    """
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(**kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(recording)
        return real_client(**kwargs)  # type: ignore[arg-type]

    with patch("xnatctl.services.uploads.httpx.Client", factory):
        yield seen


def ok(request: httpx.Request) -> httpx.Response:
    """Accept every request: JSESSION login and import alike."""
    if request.url.path == "/data/JSESSION":
        return httpx.Response(200, text="TOKEN-FROM-LOGIN")
    return httpx.Response(200, text="OK")


def imports(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.url.path == "/data/services/import"]


def params_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The upload retry ladder sleeps 2+4+8+16+32s per failing batch.

    The wait is ``CancellationToken.sleep`` rather than ``time.sleep`` so an
    interrupt does not have to be waited out; patch it at that seam, or the
    backoff is real and this file takes a minute per failing batch.
    """
    monkeypatch.setattr(
        "xnatctl.core.cancellation.CancellationToken.sleep", lambda _self, _s: False
    )


@pytest.fixture
def service() -> UploadService:
    client = MagicMock(spec=XNATClient)
    client.base_url = "https://xnat.example.org"
    client.session_token = "CACHED-TOKEN"
    client.verify_ssl = True
    client.username = "admin"
    client.password = "hunter2"
    return UploadService(client)


@pytest.fixture
def dicom_tree(tmp_path: Path) -> Path:
    """Eight DICOM-like files across two scan directories."""
    root = tmp_path / "dicoms"
    for scan in ("scan1", "scan2"):
        d = root / scan
        d.mkdir(parents=True)
        for i in range(4):
            (d / f"{scan}_{i:03d}.dcm").write_bytes(b"\x00" * 128 + b"DICM" + bytes([i]))
    return root


# =============================================================================
# Batch splitting
# =============================================================================


def test_files_are_split_across_the_requested_workers(
    service: UploadService, dicom_tree: Path
) -> None:
    with mock_uploads_http(ok) as seen:
        summary = service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=4
        )

    assert summary.success is True
    assert len(imports(seen)) == 4, "one import POST per batch"
    assert summary.batches_total == 4
    assert summary.total_files == 8


def test_batch_count_never_exceeds_the_file_count(service: UploadService, tmp_path: Path) -> None:
    """Two files with eight workers must not produce six empty archives."""
    root = tmp_path / "small"
    root.mkdir()
    for i in range(2):
        (root / f"f{i}.dcm").write_bytes(b"\x00" * 128 + b"DICM")

    with mock_uploads_http(ok) as seen:
        summary = service.upload_dicom_parallel(root, "PROJ", "SUB001", "SESS01", upload_workers=8)

    assert len(imports(seen)) == 2
    assert summary.batches_total == 2


def test_every_collected_file_lands_in_exactly_one_archive(
    service: UploadService, dicom_tree: Path
) -> None:
    """The split must partition the file set -- no duplicates, no drops."""
    members: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/services/import":
            with tarfile.open(fileobj=io.BytesIO(request.read()), mode="r:") as tar:
                members.extend(m.name for m in tar.getmembers() if m.isfile())
        return ok(request)

    with mock_uploads_http(capture):
        service.upload_dicom_parallel(dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=3)

    assert len(members) == 8
    assert len(set(members)) == 8, "a file was archived twice"


# =============================================================================
# Import parameters -- what XNAT actually receives
# =============================================================================


def test_import_params_carry_the_target_identifiers(
    service: UploadService, dicom_tree: Path
) -> None:
    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(dicom_tree, "MYPROJ", "SUB042", "SESSION_X", upload_workers=1)

    params = params_of(imports(seen)[0])
    assert params["project"] == "MYPROJ"
    assert params["subject"] == "SUB042"
    assert params["session"] == "SESSION_X"
    assert params["import-handler"] == "DICOM-zip"
    assert params["inbody"] == "true"


def test_overwrite_mode_is_forwarded_verbatim(service: UploadService, dicom_tree: Path) -> None:
    """`--overwrite append` is the additive mode; it must reach the server
    unchanged rather than being normalised away.
    """
    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1, overwrite="append"
        )

    assert params_of(imports(seen)[0])["overwrite"] == "append"


def test_direct_archive_and_prearchive_choose_different_destinations(
    service: UploadService, dicom_tree: Path
) -> None:
    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1, direct_archive=True
        )
    direct = params_of(imports(seen)[0])

    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1, direct_archive=False
        )
    prearchive = params_of(imports(seen)[0])

    assert direct != prearchive
    assert "prearchive" in str(prearchive).lower()


def test_archive_format_selects_the_content_type(service: UploadService, dicom_tree: Path) -> None:
    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1, archive_format="zip"
        )

    request = imports(seen)[0]
    assert request.headers["content-type"] == "application/zip"
    assert zipfile.is_zipfile(io.BytesIO(request.read()))


def test_tar_is_the_default_archive_format(service: UploadService, dicom_tree: Path) -> None:
    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1)

    assert imports(seen)[0].headers["content-type"] == "application/x-tar"


# =============================================================================
# Authentication
# =============================================================================


def test_cached_session_token_is_reused_without_logging_in(
    service: UploadService, dicom_tree: Path
) -> None:
    with mock_uploads_http(ok) as seen:
        service.upload_dicom_parallel(dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=2)

    assert not [r for r in seen if r.url.path == "/data/JSESSION"]
    assert "CACHED-TOKEN" in imports(seen)[0].headers.get("cookie", "")


def test_without_a_token_each_worker_logs_in_and_logs_out(
    service: UploadService, dicom_tree: Path
) -> None:
    """Per-thread sessions are the thread-safety design; the important part is
    that a worker-created session is released rather than leaked.
    """
    service.client.session_token = None

    with mock_uploads_http(ok) as seen:
        summary = service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=2
        )

    assert summary.success is True
    logins = [r for r in seen if r.url.path == "/data/JSESSION" and r.method == "POST"]
    logouts = [r for r in seen if r.url.path == "/data/JSESSION" and r.method == "DELETE"]
    assert len(logins) == 2
    assert len(logouts) == len(logins), "worker sessions must not leak"


def test_missing_credentials_fail_the_batch_without_uploading(
    service: UploadService, dicom_tree: Path
) -> None:
    service.client.session_token = None
    service.client.username = None
    service.client.password = None

    with mock_uploads_http(ok) as seen:
        summary = service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1
        )

    assert summary.success is False
    assert imports(seen) == []
    assert any("missing credentials" in e for e in summary.errors)


# =============================================================================
# Failure aggregation
# =============================================================================


def test_one_failing_batch_is_reported_without_failing_the_rest(
    service: UploadService, dicom_tree: Path
) -> None:
    doomed: dict[str, str] = {}
    lock = threading.Lock()

    def one_bad(request: httpx.Request) -> httpx.Response:
        """Fail one specific archive, identified by its bytes.

        Keyed on the body rather than a call counter so the doomed batch keeps
        failing across the retry ladder -- a counter would let attempt 2
        succeed and the test would assert nothing.
        """
        if request.url.path != "/data/services/import":
            return ok(request)
        key = hashlib.sha1(request.read()).hexdigest()
        with lock:
            doomed.setdefault("key", key)
            is_doomed = doomed["key"] == key
        if is_doomed:
            return httpx.Response(500, text="server exploded")
        return httpx.Response(200, text="OK")

    with mock_uploads_http(one_bad):
        summary = service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=3
        )

    assert summary.success is False
    assert summary.batches_failed == 1
    assert summary.batches_succeeded == 2
    assert len(summary.errors) == 1
    assert "500" in summary.errors[0]


def test_auth_rejection_is_reported_as_such(service: UploadService, dicom_tree: Path) -> None:
    with mock_uploads_http(lambda r: httpx.Response(401, text="nope")):
        summary = service.upload_dicom_parallel(
            dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=1
        )

    assert summary.success is False
    assert "expired session" in summary.errors[0] or "Authentication" in summary.errors[0]


def test_empty_directory_fails_cleanly(service: UploadService, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with mock_uploads_http(ok) as seen:
        summary = service.upload_dicom_parallel(empty, "PROJ", "SUB001", "SESS01")

    assert summary.success is False
    assert summary.total == 0
    assert summary.errors == ["No DICOM files found"]
    assert seen == [], "nothing should reach the network"


# =============================================================================
# Progress reporting
# =============================================================================


def test_progress_reports_preparing_then_uploading_then_complete(
    service: UploadService, dicom_tree: Path
) -> None:
    seen_progress: list[UploadProgress] = []

    with mock_uploads_http(ok):
        service.upload_dicom_parallel(
            dicom_tree,
            "PROJ",
            "SUB001",
            "SESS01",
            upload_workers=2,
            progress_callback=seen_progress.append,
        )

    phases = [p.phase for p in seen_progress]
    assert phases[0] is OperationPhase.PREPARING
    assert OperationPhase.UPLOADING in phases
    assert phases[-1] is OperationPhase.COMPLETE


def test_failure_ends_in_the_error_phase(service: UploadService, dicom_tree: Path) -> None:
    seen_progress: list[UploadProgress] = []

    with mock_uploads_http(lambda r: httpx.Response(500, text="boom")):
        service.upload_dicom_parallel(
            dicom_tree,
            "PROJ",
            "SUB001",
            "SESS01",
            upload_workers=1,
            progress_callback=seen_progress.append,
        )

    assert seen_progress[-1].phase is OperationPhase.ERROR


def test_progress_counts_every_completed_batch(service: UploadService, dicom_tree: Path) -> None:
    seen_progress: list[UploadProgress] = []

    with mock_uploads_http(ok):
        service.upload_dicom_parallel(
            dicom_tree,
            "PROJ",
            "SUB001",
            "SESS01",
            upload_workers=4,
            progress_callback=seen_progress.append,
        )

    uploading = [p for p in seen_progress if p.phase is OperationPhase.UPLOADING and p.current]
    assert [p.current for p in uploading] == [1, 2, 3, 4]


# =============================================================================
# Temp-archive lifecycle
# =============================================================================


def test_archives_are_cleaned_up_on_success(
    service: UploadService, dicom_tree: Path, tmp_path: Path
) -> None:
    """Archives are a copy of the whole dataset; leaving them behind would
    double the disk footprint of every upload.
    """
    made: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        made.append(Path(path))
        return path

    with (
        patch("xnatctl.services.uploads.tempfile.mkdtemp", tracking),
        mock_uploads_http(ok),
    ):
        service.upload_dicom_parallel(dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=2)

    assert made, "the transport should have created a workspace"
    for path in made:
        assert not path.exists(), f"leaked upload workspace: {path}"


def test_archives_are_cleaned_up_after_a_failure(service: UploadService, dicom_tree: Path) -> None:
    made: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        made.append(Path(path))
        return path

    with (
        patch("xnatctl.services.uploads.tempfile.mkdtemp", tracking),
        mock_uploads_http(lambda r: httpx.Response(500, text="boom")),
    ):
        service.upload_dicom_parallel(dicom_tree, "PROJ", "SUB001", "SESS01", upload_workers=2)

    for path in made:
        assert not path.exists(), f"leaked upload workspace on the failure path: {path}"
