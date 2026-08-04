"""Coverage for the DICOM C-STORE upload transport.

`upload_dicom_store` is the scanner-integration path and had no test at all:
C-ECHO gating, batching across parallel associations, SOP UID backfill, the
sent/failed tallies, and workspace cleanup were all unverified.

pydicom/pynetdicom are injected as fakes rather than gated behind
``pytest.importorskip``. The optional extra is not installed in the default
dev environment, so a skip would mean these never run -- and the logic under
test is xnatctl's orchestration, not the DICOM libraries' behaviour. Real
library integration stays a separate concern.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from xnatctl.services.uploads import UploadService

SUCCESS = 0x0000
FAILURE = 0xC000


class FakeStatus:
    def __init__(self, status: int = SUCCESS):
        self.Status = status


class FakeDataset:
    """Stand-in for a pydicom Dataset, with the file_meta split that matters."""

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
    """Records every association so tests can inspect what was sent."""

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
        # A C-ECHO association only ever requests the verification context.
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
    """Inject minimal pydicom/pynetdicom modules for the duration of a test."""
    FakeAE.reset()

    class InvalidDicomError(Exception):
        pass

    pydicom_errors = types.ModuleType("pydicom.errors")
    pydicom_errors.InvalidDicomError = InvalidDicomError  # type: ignore[attr-defined]

    pydicom = types.ModuleType("pydicom")
    pydicom.errors = pydicom_errors  # type: ignore[attr-defined]
    pydicom.dcmread = lambda path, force=False: FakeDataset()  # type: ignore[attr-defined]

    sop_class = types.ModuleType("pynetdicom.sop_class")
    sop_class.Verification = "1.2.840.10008.1.1"  # type: ignore[attr-defined]

    pynetdicom = types.ModuleType("pynetdicom")
    pynetdicom.AE = FakeAE  # type: ignore[attr-defined]
    pynetdicom.sop_class = sop_class  # type: ignore[attr-defined]
    pynetdicom.StoragePresentationContexts = ["ctx-a", "ctx-b"]  # type: ignore[attr-defined]

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


# =============================================================================
# C-ECHO gating
# =============================================================================


def test_failed_c_echo_aborts_before_any_store(service: UploadService, dicom_root: Path) -> None:
    """A refused association means wrong host/port/AET; sending anyway would
    produce a slow pile of failures instead of one clear error."""
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


# =============================================================================
# Batching and association lifecycle
# =============================================================================


def test_files_are_split_across_parallel_associations(
    service: UploadService, dicom_root: Path
) -> None:
    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=3)

    store_assocs = [a for ae in FakeAE.instances for a in ae.associations if a.stored]
    assert len(store_assocs) == 3
    assert sum(len(a.stored) for a in store_assocs) == 6


def test_every_association_is_released(service: UploadService, dicom_root: Path) -> None:
    """A leaked association holds a slot on the scanner's SCP."""
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


# =============================================================================
# SOP UID backfill
# =============================================================================


def test_missing_sop_uids_are_filled_from_file_meta(
    service: UploadService, dicom_root: Path
) -> None:
    """Datasets read with force=True can lack top-level SOP UIDs; sending one
    without them is rejected by the SCP."""
    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    sent = [ds for ae in FakeAE.instances for a in ae.associations for ds in a.stored]
    assert sent
    for ds in sent:
        assert ds.SOPClassUID == "1.2.840.10008.5.1.4.1.1.4"
        assert ds.SOPInstanceUID == "1.2.3.4.5.6"


def test_existing_sop_uids_are_left_alone(service: UploadService, dicom_root: Path) -> None:
    import pydicom  # the injected fake

    pydicom.dcmread = lambda path, force=False: FakeDataset(  # type: ignore[attr-defined]
        sop_class="ORIGINAL-CLASS", sop_instance="ORIGINAL-INSTANCE"
    )

    service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    sent = [ds for ae in FakeAE.instances for a in ae.associations for ds in a.stored]
    assert all(ds.SOPClassUID == "ORIGINAL-CLASS" for ds in sent)


# =============================================================================
# Tallies and error paths
# =============================================================================


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
    import pydicom  # the injected fake

    calls = {"n": 0}

    def flaky_read(path: Path, force: bool = False) -> FakeDataset:
        calls["n"] += 1
        if calls["n"] == 1:
            raise pydicom.errors.InvalidDicomError("not a DICOM file")
        return FakeDataset()

    pydicom.dcmread = flaky_read  # type: ignore[attr-defined]

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


# =============================================================================
# Workspace lifecycle
# =============================================================================


def test_workspace_is_removed_after_a_clean_run(service: UploadService, dicom_root: Path) -> None:
    summary = service.upload_dicom_store(dicom_root, "host", "XNAT", workers=1)

    assert not summary.workspace.exists()


def test_workspace_is_kept_when_something_failed(service: UploadService, dicom_root: Path) -> None:
    """The per-batch logs are the only record of which files failed, so a
    failed run must not delete them."""
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


# =============================================================================
# Missing optional dependency
# =============================================================================


def test_missing_dicom_extra_raises_with_the_install_hint(
    service: UploadService, dicom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pynetdicom", None)

    with pytest.raises(ImportError, match=r"xnatctl\[dicom\]"):
        service.upload_dicom_store(dicom_root, "host", "XNAT")


def test_no_association_is_attempted_without_the_extra(
    service: UploadService, dicom_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pydicom", None)

    with pytest.raises(ImportError):
        service.upload_dicom_store(dicom_root, "host", "XNAT")

    assert FakeAE.instances == []


def test_patch_target_is_the_module_not_an_attribute() -> None:
    """`AE` is imported inside the function bodies, so
    `xnatctl.services.uploads.AE` never exists -- patching it would silently
    do nothing. This guards the fixture's approach."""
    import xnatctl.services.uploads as uploads_mod

    assert not hasattr(uploads_mod, "AE")
    with pytest.raises(AttributeError):
        with patch.object(uploads_mod, "AE"):
            pass
