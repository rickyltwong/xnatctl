"""Coverage for the gradual-DICOM upload scheduler.

The gradual transport sends one HTTP request per file, and the order it picks
is deliberate rather than incidental:

* a small sequential *warm-up* runs before going wide, because XNAT returns
  transient 400s while a session/scan is still being created in the
  prearchive, and several workers hitting that cold start at once turns one
  retry into many;
* the remaining files are then interleaved *round-robin across scans*, so XNAT
  builds every scan concurrently instead of finishing scan 1 before scan 2
  starts.

Both were untested. These drive the real scheduler with the per-file uploader
faked out, so the assertions are about ordering and accounting.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import xnatctl.services.upload.gradual as uploads
from xnatctl.models.progress import OperationPhase, UploadProgress
from xnatctl.services.upload import UploadService


@pytest.fixture
def service() -> UploadService:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    client.session_token = "TOKEN"
    client.username = "admin"
    client.password = "hunter2"
    client.verify_ssl = True
    client.server_version = None
    return UploadService(client)


def session_tree(root: Path, scans: dict[str, int]) -> Path:
    """Build the standard `scans/<id>/resources/DICOM/files/` session layout."""
    for scan_id, count in scans.items():
        files_dir = root / "scans" / scan_id / "resources" / "DICOM" / "files"
        files_dir.mkdir(parents=True)
        for i in range(count):
            (files_dir / f"{scan_id}_{i:03d}.dcm").write_bytes(b"\x00" * 128 + b"DICM")
    return root


def record_uploads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: Callable[[Path], bool] = lambda _p: False,
) -> list[Path]:
    """Fake the per-file uploader, recording call order."""
    order: list[Path] = []

    def fake_upload_one(**kwargs: object) -> tuple[str, bool, str]:
        path = kwargs["file_path"]
        assert isinstance(path, Path)
        order.append(path)
        display = kwargs.get("display_path") or path.name
        if fail(path):
            return (str(display), False, "HTTP 500: boom")
        return (str(display), True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)
    return order


def scan_of(path: Path) -> str:
    """Recover the scan id from a session-layout path."""
    parts = path.parts
    return parts[parts.index("scans") + 1]


# =============================================================================
# Round-robin interleaving
# =============================================================================


def test_files_are_interleaved_across_scans(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploading scan 1 to completion before starting scan 2 would leave XNAT
    building one scan at a time.
    """
    root = session_tree(tmp_path / "sess", {"1": 4, "2": 4, "3": 4})
    order = record_uploads(monkeypatch)

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=1
    )

    assert summary.success is True
    assert len(order) == 12

    # After the warm-up (one file per scan), the tail must alternate rather
    # than run in blocks.
    tail = [scan_of(p) for p in order[3:]]
    assert tail == ["1", "2", "3", "1", "2", "3", "1", "2", "3"]


def test_warmup_takes_one_file_from_each_scan_first(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm-up exists to create each scan once, quietly, before the wide
    phase can pile onto a cold prearchive.
    """
    root = session_tree(tmp_path / "sess", {"1": 5, "2": 5})
    order = record_uploads(monkeypatch)

    service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=4
    )

    assert sorted(scan_of(p) for p in order[:2]) == ["1", "2"]


def test_uneven_scans_drain_without_dropping_files(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short scan empties early; the round-robin must drop that queue and
    keep cycling the rest rather than stalling or repeating.
    """
    root = session_tree(tmp_path / "sess", {"1": 1, "2": 5, "3": 2})
    order = record_uploads(monkeypatch)

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=2
    )

    assert summary.success is True
    assert len(order) == 8
    assert len(set(order)) == 8, "a file was uploaded twice"
    counts = {s: sum(1 for p in order if scan_of(p) == s) for s in ("1", "2", "3")}
    assert counts == {"1": 1, "2": 5, "3": 2}


def test_files_outside_the_scan_layout_are_still_uploaded(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loose files have no scan id; they go in an `_other` queue rather than
    being silently skipped.
    """
    root = session_tree(tmp_path / "sess", {"1": 2})
    (root / "loose_a.dcm").write_bytes(b"\x00" * 128 + b"DICM")
    (root / "loose_b.dcm").write_bytes(b"\x00" * 128 + b"DICM")
    order = record_uploads(monkeypatch)

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=1
    )

    assert summary.success is True
    names = {p.name for p in order}
    assert "loose_a.dcm" in names
    assert "loose_b.dcm" in names


def test_numeric_scan_ids_sort_numerically_not_lexically(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lexical ordering would put scan 10 before scan 2."""
    root = session_tree(tmp_path / "sess", {"2": 1, "10": 1, "1": 1})
    order = record_uploads(monkeypatch)

    service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=1
    )

    assert [scan_of(p) for p in order] == ["1", "2", "10"]


# =============================================================================
# Accounting
# =============================================================================


def test_per_file_failures_are_tallied_not_raised(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad file must not abort an upload of thousands -- that is the whole
    reason to prefer gradual over a single batch archive.
    """
    root = session_tree(tmp_path / "sess", {"1": 4})
    order = record_uploads(monkeypatch, fail=lambda p: p.name.endswith("_002.dcm"))

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=2
    )

    assert summary.success is False
    assert summary.failed == 1
    assert summary.succeeded == 3
    assert any("_002.dcm" in e for e in summary.errors)
    # The healthy files are attempted exactly once.
    assert sum(1 for p in order if not p.name.endswith("_002.dcm")) == 3


def test_a_persistently_failing_file_is_retried_twice_then_given_up_on(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failures get two further passes -- one at reduced concurrency, then a
    sequential one -- because the usual cause is XNAT contention rather than a
    bad file. Three attempts total, and then it is reported, not retried
    forever.
    """
    root = session_tree(tmp_path / "sess", {"1": 2})
    order = record_uploads(monkeypatch, fail=lambda p: p.name.endswith("_001.dcm"))

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=2
    )

    attempts = [p for p in order if p.name.endswith("_001.dcm")]
    assert len(attempts) == 3
    assert summary.failed == 1


def test_a_file_that_recovers_on_retry_counts_as_succeeded(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry passes exist to absorb transient prearchive 400s; a file that
    works the second time must not be reported as a failure.
    """
    root = session_tree(tmp_path / "sess", {"1": 3})
    seen: dict[str, int] = {}

    def fail_first_attempt_only(path: Path) -> bool:
        if not path.name.endswith("_001.dcm"):
            return False
        seen[path.name] = seen.get(path.name, 0) + 1
        return seen[path.name] == 1

    record_uploads(monkeypatch, fail=fail_first_attempt_only)

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=2
    )

    assert summary.success is True
    assert summary.failed == 0
    assert summary.succeeded == 3
    assert summary.errors == []


def test_a_fully_successful_run_reports_success(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = session_tree(tmp_path / "sess", {"1": 3, "2": 3})
    record_uploads(monkeypatch)

    summary = service.upload_dicom_gradual(
        root, project="PROJ", subject="SUB001", session="SESS01", workers=3
    )

    assert summary.success is True
    assert summary.succeeded == 6
    assert summary.failed == 0
    assert summary.errors == []


def test_progress_runs_from_preparing_to_complete(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = session_tree(tmp_path / "sess", {"1": 3})
    record_uploads(monkeypatch)
    seen: list[UploadProgress] = []

    service.upload_dicom_gradual(
        root,
        project="PROJ",
        subject="SUB001",
        session="SESS01",
        workers=2,
        progress_callback=seen.append,
    )

    phases = [p.phase for p in seen]
    assert phases[0] is OperationPhase.PREPARING
    assert phases[-1] is OperationPhase.COMPLETE


def test_failure_ends_in_the_error_phase(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = session_tree(tmp_path / "sess", {"1": 2})
    record_uploads(monkeypatch, fail=lambda _p: True)
    seen: list[UploadProgress] = []

    service.upload_dicom_gradual(
        root,
        project="PROJ",
        subject="SUB001",
        session="SESS01",
        workers=1,
        progress_callback=seen.append,
    )

    assert seen[-1].phase is OperationPhase.ERROR


def test_empty_directory_reports_failure_without_uploading(
    service: UploadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    order = record_uploads(monkeypatch)

    summary = service.upload_dicom_gradual(
        empty, project="PROJ", subject="SUB001", session="SESS01"
    )

    assert summary.success is False
    assert order == []


# =============================================================================
# Instance reuse (GradualUploadRun.run() must not leak state between calls)
# =============================================================================


def test_reusing_a_run_instance_does_not_leak_state_between_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second ``run()`` on the same ``GradualUploadRun`` must start clean.

    ``UploadService`` always builds a fresh ``GradualUploadRun`` per call, so
    this never happens through the CLI or the service layer -- but the class
    is directly importable, and nothing about its constructor signals that an
    instance is single-use. Without a reset, a second run would retry the
    first run's failures, add the first run's ``completed`` count on top of
    the second run's (impossible progress like "5/2"), and report failures
    for files that were never part of the second run at all.
    """
    from xnatctl.services.upload.gradual import GradualUploadRun
    from xnatctl.services.upload.gradual_client import GradualClientPool

    first_dir = tmp_path / "first"
    first_dir.mkdir()
    good1 = first_dir / "good1.dcm"
    bad1 = first_dir / "bad.dcm"
    good1.write_bytes(b"\x00" * 128 + b"DICM")
    bad1.write_bytes(b"\x00" * 128 + b"DICM")

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    good2 = second_dir / "good2.dcm"
    good3 = second_dir / "good3.dcm"
    good2.write_bytes(b"\x00" * 128 + b"DICM")
    good3.write_bytes(b"\x00" * 128 + b"DICM")

    def fake_upload_one(**kwargs: object) -> tuple[str, bool, str]:
        path = kwargs["file_path"]
        assert isinstance(path, Path)
        if path.name == "bad.dcm":
            return (path.name, False, "boom")
        return (path.name, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    client.session_token = "TOKEN"
    client.username = "admin"
    client.password = "hunter2"
    client.verify_ssl = True
    client.server_version = None

    run = GradualUploadRun(
        client=client,
        pool=GradualClientPool(),
        project="PROJ",
        subject="SUB001",
        session="SESS01",
        direct_archive=True,
        display_root=first_dir,
        progress_callback=None,
        start_time=0.0,
    )

    summary1 = run.run([good1, bad1], workers=2)
    assert summary1.total == 2
    assert summary1.failed == 1
    assert summary1.succeeded == 1

    run.display_root = second_dir
    summary2 = run.run([good2, good3], workers=2)

    assert summary2.total == 2, "second run must count only its own files"
    assert summary2.succeeded == 2, "first run's completed count leaked into the second"
    assert summary2.failed == 0, "first run's failure (bad.dcm) leaked into the second"
    assert summary2.errors == [], "first run's error leaked into the second run's summary"
    # start_time was constructed as 0.0 (epoch); if run() failed to re-stamp
    # it for this second call, duration would be ~time.time() (billions of
    # seconds), not a fraction of a second.
    assert summary2.duration < 5, "the first run's start_time leaked into the second"


# =============================================================================
# Display-path fallbacks (paths outside display_root raise ValueError)
# =============================================================================


def _bare_run(tmp_path: Path) -> uploads.GradualUploadRun:
    from xnatctl.services.upload.gradual import GradualUploadRun
    from xnatctl.services.upload.gradual_client import GradualClientPool

    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    client.session_token = "TOKEN"
    client.username = "admin"
    client.password = "hunter2"
    client.verify_ssl = True
    client.server_version = None
    return GradualUploadRun(
        client=client,
        pool=GradualClientPool(),
        project="PROJ",
        subject="SUB001",
        session="SESS01",
        direct_archive=True,
        display_root=tmp_path / "root",
        progress_callback=None,
        start_time=0.0,
    )


def test_display_falls_back_to_name_for_paths_outside_the_root(tmp_path: Path) -> None:
    """`relative_to` raises ValueError for a path outside display_root."""
    run = _bare_run(tmp_path)
    outside = tmp_path / "elsewhere" / "file.dcm"
    assert run.display(outside) == "file.dcm"


def test_scan_id_is_none_for_paths_outside_the_root(tmp_path: Path) -> None:
    run = _bare_run(tmp_path)
    outside = tmp_path / "elsewhere" / "scans" / "3" / "resources" / "DICOM" / "files" / "f.dcm"
    assert run._scan_id_for(outside) is None
