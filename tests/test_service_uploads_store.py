from __future__ import annotations

import logging
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from xnatctl.services.upload import UploadService

SUCCESS = 0x0000
FAILURE = 0xC000


class FakeStatus:
    def __init__(self, status: int = SUCCESS):
        self.Status = status


class FakeDataset:
    def __init__(self, *, sop_class: str | None = None, sop_instance: str | None = None):
        self.SOPClassUID = sop_class
        self.SOPInstanceUID = sop_instance
        self.file_meta = types.SimpleNamespace(
            MediaStorageSOPClassUID="1.2.840.10008.5.1.4.1.1.4",
            MediaStorageSOPInstanceUID="1.2.3.4.5.6",
        )


class FakeAssociation:
    def __init__(self, *, established: bool = True, store_status: int = SUCCESS):
        self.is_established = established
        self._store_status = store_status
        self.released = False
        self.stored: list[object] = []

    def send_c_echo(self) -> FakeStatus:
        return FakeStatus(SUCCESS)

    def send_c_store(self, ds: object) -> FakeStatus:
        self.stored.append(ds)
        return FakeStatus(self._store_status)

    def release(self) -> None:
        self.released = True


class FakeAE:
    instances: list[FakeAE] = []
    echo_established = True
    store_established = True
    store_status = SUCCESS

    def __init__(self, ae_title: str = "XNATCTL"):
        self.ae_title = ae_title
        self.requested_contexts: list[object] = []
        self.associations: list[FakeAssociation] = []
        FakeAE.instances.append(self)

    def add_requested_context(self, context: object) -> None:
        self.requested_contexts.append(context)

    def associate(self, host: str, port: int, ae_title: str = "") -> FakeAssociation:
        is_echo = self.requested_contexts == ["1.2.840.10008.1.1"]
        assoc = FakeAssociation(
            established=FakeAE.echo_established if is_echo else FakeAE.store_established,
            store_status=FakeAE.store_status,
        )
        self.associations.append(assoc)
        return assoc

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.echo_established = True
        cls.store_established = True
        cls.store_status = SUCCESS


@pytest.fixture(autouse=True)
def fake_dicom_libs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeAE.reset()

    class InvalidDicomError(Exception):
        pass

    pydicom_errors = types.ModuleType("pydicom.errors")
    pydicom_errors.InvalidDicomError = InvalidDicomError

    pydicom = types.ModuleType("pydicom")
    pydicom.errors = pydicom_errors
    pydicom.dcmread = lambda path, force=False: FakeDataset()

    sop_class = types.ModuleType("pynetdicom.sop_class")
    sop_class.Verification = "1.2.840.10008.1.1"

    pynetdicom = types.ModuleType("pynetdicom")
    pynetdicom.AE = FakeAE
    pynetdicom.sop_class = sop_class
    pynetdicom.StoragePresentationContexts = ["ctx-a", "ctx-b"]

    for name, module in {
        "pydicom": pydicom,
        "pydicom.errors": pydicom_errors,
        "pynetdicom": pynetdicom,
        "pynetdicom.sop_class": sop_class,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    yield


@pytest.fixture
def service() -> UploadService:
    from unittest.mock import MagicMock

    return UploadService(MagicMock())


@pytest.fixture
def dicom_root(tmp_path: Path) -> Path:
    root = tmp_path / "dicoms"
    root.mkdir()
    for i in range(6):
        (root / f"img{i:03d}.dcm").write_bytes(b"\x00" * 128 + b"DICM")
    return root


def test_failed_c_echo_aborts_before_any_store(service: UploadService, dicom_root: Path) -> None:
    FakeAE.echo_established = False

    with pytest.raises(RuntimeError, match="C-ECHO failed"):
        service.upload_dicom_store(dicom_root, "scanner.example.org", "XNAT")

    assert all(not a.stored for ae in FakeAE.instances for a in ae.associations)


def test_c_echo_error_names_the_connection_settings(
    service: UploadService, dicom_root: Path
) -> None:
    FakeAE.echo_established = False

    with pytest.raises(RuntimeError) as exc_info:
        service.upload_dicom_store(
            dicom_root, "scanner.example.org", "XNATAE", port=11112, calling_aet="ME"
        )

    message = str(exc_info.value)
    assert "11112" in message
    assert "XNATAE" in message


def test_successful_echo_proceeds_to_store(service: UploadService, dicom_root: Path) -> None:
    summary = service.upload_dicom_store(dicom_root, "scanner.example.org", "XNAT", workers=2)

    assert summary.success is True
    assert summary.sent == 6
    assert summary.failed == 0


def test_files_are_split_across_parallel_associations(
    service: UploadService, dicom_root: Path
) -> None:
    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=3)

    store_assocs = [a for ae in FakeAE.instances for a in ae.associations if a.stored]
    assert len(store_assocs) == 3
    assert sum(len(a.stored) for a in store_assocs) == 6


def test_every_association_is_released(service: UploadService, dicom_root: Path) -> None:
    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=2)

    established = [a for ae in FakeAE.instances for a in ae.associations if a.is_established]
    assert established
    assert all(a.released for a in established)


def test_rejected_store_association_counts_its_whole_batch_as_failed(
    service: UploadService, dicom_root: Path
) -> None:
    FakeAE.store_established = False

    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=2)

    assert summary.success is False
    assert summary.sent == 0
    assert summary.failed == 6


def test_missing_sop_uids_are_filled_from_file_meta(
    service: UploadService, dicom_root: Path
) -> None:
    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    sent = [ds for ae in FakeAE.instances for a in ae.associations for ds in a.stored]
    assert sent
    for ds in sent:
        assert ds.SOPClassUID == "1.2.840.10008.5.1.4.1.1.4"
        assert ds.SOPInstanceUID == "1.2.3.4.5.6"


def test_existing_sop_uids_are_left_alone(service: UploadService, dicom_root: Path) -> None:
    import pydicom

    pydicom.dcmread = lambda path, force=False: FakeDataset(
        sop_class="ORIGINAL-CLASS", sop_instance="ORIGINAL-INSTANCE"
    )

    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    sent = [ds for ae in FakeAE.instances for a in ae.associations for ds in a.stored]
    assert all(ds.SOPClassUID == "ORIGINAL-CLASS" for ds in sent)


def test_store_failures_are_counted_not_raised(service: UploadService, dicom_root: Path) -> None:
    FakeAE.store_status = FAILURE

    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=2)

    assert summary.success is False
    assert summary.sent == 0
    assert summary.failed == 6
    assert summary.total_files == 6


def test_unreadable_file_is_counted_as_failed_and_skipped(
    service: UploadService, dicom_root: Path
) -> None:
    import pydicom

    calls = {"n": 0}

    def flaky_read(path: Path, force: bool = False) -> FakeDataset:
        calls["n"] += 1
        if calls["n"] == 1:
            raise pydicom.errors.InvalidDicomError("not a DICOM file")
        return FakeDataset()

    pydicom.dcmread = flaky_read

    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    assert summary.failed == 1
    assert summary.sent == 5


def test_empty_directory_raises_rather_than_reporting_success(
    service: UploadService, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(RuntimeError, match="No DICOM files"):
        service.upload_dicom_store(empty, "host", "XNAT")


def test_a_missing_directory_is_rejected_up_front(service: UploadService, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        service.upload_dicom_store(tmp_path / "nope", "host", "XNAT")


def test_workspace_is_removed_after_a_clean_run(service: UploadService, dicom_root: Path) -> None:
    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    assert not summary.workspace.exists()


def test_workspace_is_kept_when_something_failed(service: UploadService, dicom_root: Path) -> None:
    FakeAE.store_status = FAILURE

    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    assert summary.workspace.exists()
    assert summary.log_dir.exists()
    logs = list(summary.log_dir.glob("*.log"))
    assert logs, "a failed run must leave per-batch logs behind"
    assert "Failed" in logs[0].read_text()


def test_cleanup_can_be_disabled(service: UploadService, dicom_root: Path) -> None:
    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1, cleanup=False)

    assert summary.workspace.exists()


def test_patch_target_is_the_module_not_an_attribute() -> None:
    import xnatctl.services.upload.dicom_store as dicom_store_mod

    assert not hasattr(dicom_store_mod, "AE")
    with pytest.raises(AttributeError):
        with patch.object(dicom_store_mod, "AE"):
            pass


def test_a_failed_send_keeps_the_exception_text_out_of_the_log_record(
    service: UploadService, dicom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "1.2.826.0.1.3680043.99.42"

    def failing_send(self: FakeAssociation, ds: object) -> FakeStatus:
        raise ValueError(f"no presentation context for {uid}")

    monkeypatch.setattr(FakeAssociation, "send_c_store", failing_send)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    store_logger = logging.getLogger("xnatctl.services.upload.dicom_store")
    capture = Capture()
    store_logger.addHandler(capture)
    try:
        summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)
    finally:
        store_logger.removeHandler(capture)

    assert summary.failed == 6
    failures = [r for r in records if r.exc_info]
    assert len(failures) == 6
    for record in failures:
        assert record.exc_info is not None
        assert type(record.exc_info[1]).__name__ == "DetailsWithheldError"
        assert uid not in logging.Formatter().format(record)
