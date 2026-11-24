from __future__ import annotations

import hashlib
import io
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]

SCAN_IDS = ("1", "2")
SLICES_PER_SCAN = 3


def _make_session(root: Path, subject: str, session: str) -> dict[str, str]:
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    assert pydicom
    checksums: dict[str, str] = {}
    study_uid = generate_uid()

    for scan_id in SCAN_IDS:
        series_uid = generate_uid()
        for index in range(SLICES_PER_SCAN):
            ds = Dataset()
            ds.file_meta = FileMetaDataset()
            ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
            ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

            ds.SOPClassUID = CTImageStorage
            ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
            ds.StudyInstanceUID = study_uid
            ds.SeriesInstanceUID = series_uid
            ds.SeriesNumber = int(scan_id)
            ds.InstanceNumber = index + 1
            ds.Modality = "CT"
            ds.PatientID = subject
            ds.PatientName = subject
            ds.StudyDate = "20200101"
            ds.StudyTime = "120000"
            ds.StudyDescription = session
            ds.SeriesDescription = f"scan{scan_id}"
            ds.AccessionNumber = session

            ds.Rows = 4
            ds.Columns = 4
            ds.BitsAllocated = 16
            ds.BitsStored = 16
            ds.HighBit = 15
            ds.PixelRepresentation = 0
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.PixelData = bytes(range(32))

            path = root / f"scan{scan_id}" / f"{index + 1:04d}.dcm"
            path.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(path, enforce_file_format=True)
            checksums[str(ds.SOPInstanceUID)] = hashlib.sha256(ds.PixelData).hexdigest()

    return checksums


def _poll(predicate: Any, timeout: float, interval: float = 3.0, what: str = "condition") -> Any:
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout}s waiting for {what} (last value: {last!r})")


class TestAuth:
    def test_login_returns_a_usable_session(self, xnat_client: Any) -> None:
        assert xnat_client.session_token

    def test_the_session_identifies_the_test_user(
        self, xnat_client: Any, credentials: tuple[str, str]
    ) -> None:
        assert xnat_client.get("/xapi/users/username").text.strip() == credentials[0]

    def test_an_invalid_token_is_rejected(self, xnat_server: str) -> None:
        from xnatctl.core.client import XNATClient
        from xnatctl.core.exceptions import AuthenticationError, SessionExpiredError

        client = XNATClient(base_url=xnat_server, session_token="not-a-real-session")
        try:
            with pytest.raises((AuthenticationError, SessionExpiredError)):
                client.get_json("/data/projects")
        finally:
            client.close()


class TestProjectLifecycle:
    def test_the_fixture_project_exists(self, xnat_client: Any, integration_project: str) -> None:
        from xnatctl.services.projects import ProjectService

        ids = {p.id for p in ProjectService(xnat_client).list()}

        assert integration_project in ids

    def test_a_subject_can_be_created_and_read_back(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        from xnatctl.services.subjects import SubjectService

        label = "SUBJ_CRUD"
        xnat_client.put(f"/data/projects/{integration_project}/subjects/{label}")

        labels = {s.label for s in SubjectService(xnat_client).list(integration_project)}

        assert label in labels


class TestRoundTrip:
    @pytest.fixture(scope="class")
    def uploaded(
        self, xnat_client: Any, integration_project: str, tmp_path_factory: Any
    ) -> dict[str, Any]:
        from xnatctl.services.upload import UploadService

        subject = "RTSUBJ"
        session = "RTSESS"
        source = tmp_path_factory.mktemp("dicom_src")
        checksums = _make_session(source, subject, session)

        summary = UploadService(xnat_client).upload_dicom_parallel(
            source_dir=source,
            project=integration_project,
            subject=subject,
            session=session,
            upload_workers=2,
            archive_format="tar",
            direct_archive=True,
        )

        assert summary.success, f"upload failed: {summary.errors}"
        return {"subject": subject, "session": session, "checksums": checksums}

    @pytest.fixture(scope="class")
    def archived(self, xnat_client: Any, integration_project: str, uploaded: dict[str, Any]) -> str:
        def find() -> str | None:
            data = xnat_client.get_json(
                f"/data/projects/{integration_project}/experiments",
                params={"format": "json"},
            )
            for row in data.get("ResultSet", {}).get("Result", []):
                if row.get("label") == uploaded["session"]:
                    return str(row["ID"])
            return None

        return str(_poll(find, timeout=600, what="the session to appear in the archive"))

    def test_the_session_archives_with_both_scans(self, xnat_client: Any, archived: str) -> None:
        from xnatctl.services.scans import ScanService

        scans = ScanService(xnat_client).list(archived)

        assert {s.id for s in scans} == set(SCAN_IDS)

    def test_the_scan_zip_has_the_layout_the_extractor_assumes(
        self, xnat_client: Any, archived: str
    ) -> None:
        resp = xnat_client.get(
            f"/data/experiments/{archived}/scans/ALL/files", params={"format": "zip"}
        )
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]

        assert names, "the archive ZIP was empty"
        assert all("/resources/" in n and "/files/" in n for n in names), names[:5]

    def test_the_image_data_survives_the_round_trip(
        self,
        xnat_client: Any,
        integration_project: str,
        archived: str,
        uploaded: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        import pydicom

        from xnatctl.services.downloads import DownloadService

        out = tmp_path / "downloaded"
        DownloadService(xnat_client).download_session_fast(
            session_project=integration_project,
            subject=uploaded["subject"],
            resolved_session_id=archived,
            session_dir=out,
            workers=2,
        )

        got = {}
        for path in out.rglob("*"):
            if not path.is_file():
                continue
            try:
                ds = pydicom.dcmread(path)
            except Exception:  # noqa: BLE001  # non-DICOM sidecars are not the subject
                continue
            got[str(ds.SOPInstanceUID)] = hashlib.sha256(ds.PixelData).hexdigest()

        expected = uploaded["checksums"]

        assert set(expected) <= set(got), (
            f"{len(set(expected) - set(got))} of {len(expected)} SOP instances did not come back"
        )
        differing = [uid for uid, digest in expected.items() if got[uid] != digest]
        assert not differing, f"pixel data changed for {len(differing)} instances"

    def test_the_download_lands_in_the_expected_layout(
        self,
        xnat_client: Any,
        integration_project: str,
        archived: str,
        uploaded: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        from xnatctl.services.downloads import DownloadService

        out = tmp_path / "layout"
        DownloadService(xnat_client).download_session_fast(
            session_project=integration_project,
            subject=uploaded["subject"],
            resolved_session_id=archived,
            session_dir=out,
            workers=2,
        )

        for scan_id in SCAN_IDS:
            scan_dir = out / "scans" / scan_id / "resources"
            assert scan_dir.is_dir(), f"missing {scan_dir}"
            assert any(scan_dir.rglob("files/*")), f"no files under {scan_dir}"


class TestPrearchive:
    def test_an_import_without_direct_archive_stops_in_the_prearchive(
        self, xnat_client: Any, integration_project: str, tmp_path: Path
    ) -> None:
        from xnatctl.services.prearchive import PrearchiveService
        from xnatctl.services.upload import UploadService

        xnat_client.put(f"/data/projects/{integration_project}/prearchive_code/0")

        subject, session = "PRESUBJ", "PRESESS"
        source = tmp_path / "pre"
        source.mkdir()
        _make_session(source, subject, session)

        summary = UploadService(xnat_client).upload_dicom_parallel(
            source_dir=source,
            project=integration_project,
            subject=subject,
            session=session,
            upload_workers=1,
            archive_format="tar",
            direct_archive=False,
        )
        assert summary.success, f"upload failed: {summary.errors}"

        def find() -> Any:
            entries = PrearchiveService(xnat_client).list(project=integration_project)
            return [e for e in entries if e.get("name") == session] or None

        assert _poll(find, timeout=600, what="the session to reach the prearchive")


class TestContainerServicePlugin:
    def test_the_container_service_plugin_is_installed(self, xnat_client: Any) -> None:
        from xnatctl.services.admin import AdminService

        rows = AdminService(xnat_client).list_plugins()

        matches = [
            row
            for row in rows
            if "container" in str(row.get("id", "")).lower()
            or "container" in str(row.get("name", "")).lower()
        ]
        assert matches, f"no Container Service plugin among installed plugins: {rows}"
