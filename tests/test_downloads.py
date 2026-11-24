from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xnatctl.core.exceptions import (
    BatchOperationError,
    DownloadError,
    ResourceNotFoundError,
    ServerError,
    SessionExpiredError,
)
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.models.progress import DownloadProgress, DownloadSummary, OperationPhase
from xnatctl.services.downloads import DownloadOutcome, DownloadService, ScanResult
from xnatctl.services.verify import VerificationManifest


class TestDownloadService:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def download_service(self, mock_client):
        return DownloadService(mock_client)

    def test_download_service_initialization(self, mock_client):
        service = DownloadService(mock_client)
        assert service.client == mock_client


class TestDownloadResourceSessionResolution:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def download_service(self, mock_client):
        return DownloadService(mock_client)

    def test_resolves_session_label_to_internal_id(self, download_service, tmp_path):
        session_label = "MY_SESSION_LABEL"
        internal_id = "XNAT_E00001"
        project = "TEST_PROJECT"

        download_service._get = MagicMock(
            return_value={"items": [{"data_fields": {"ID": internal_id}}]}
        )

        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = stream_ctx

        with pytest.raises(DownloadError):
            download_service.download_resource(
                session_id=session_label,
                resource_label="DICOM",
                output_dir=tmp_path,
                scan_id="1",
                project=project,
            )

        download_service._get.assert_called_once_with(
            f"/data/projects/{project}/experiments/{session_label}",
            params={"format": "json"},
        )

    def test_uses_session_id_directly_when_starts_with_xnat_e(self, download_service, tmp_path):
        session_id = "XNAT_E12345"
        project = "TEST_PROJECT"

        download_service._get = MagicMock()

        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = stream_ctx

        with pytest.raises(DownloadError):
            download_service.download_resource(
                session_id=session_id,
                resource_label="DICOM",
                output_dir=tmp_path,
                scan_id="1",
                project=project,
            )

        download_service._get.assert_not_called()

    def test_resolves_session_label_from_resultset(self, download_service, tmp_path):
        session_label = "SESSION_LABEL"
        internal_id = "XNAT_E99999"
        project = "PROJECT"

        download_service._get = MagicMock(
            return_value={"ResultSet": {"Result": [{"ID": internal_id}]}}
        )

        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = stream_ctx

        with pytest.raises(DownloadError):
            download_service.download_resource(
                session_id=session_label,
                resource_label="DICOM",
                output_dir=tmp_path,
                scan_id="1",
                project=project,
            )

        download_service._get.assert_called_once_with(
            f"/data/projects/{project}/experiments/{session_label}",
            params={"format": "json"},
        )

    def test_session_expired_during_resolution_propagates(self, download_service, tmp_path):
        download_service._get = MagicMock(
            side_effect=SessionExpiredError("https://xnat.example.org")
        )

        with pytest.raises(SessionExpiredError):
            download_service.download_resource(
                session_id="MY_SESSION_LABEL",
                resource_label="DICOM",
                output_dir=tmp_path,
                scan_id="1",
                project="TEST_PROJECT",
            )

    def test_server_error_during_resolution_falls_back_to_raw_id(self, download_service, tmp_path):
        download_service._get = MagicMock(
            side_effect=ServerError(503, "GET", "/data/projects/TEST_PROJECT", "unavailable")
        )

        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = stream_ctx

        with pytest.raises(DownloadError):
            download_service.download_resource(
                session_id="MY_SESSION_LABEL",
                resource_label="DICOM",
                output_dir=tmp_path,
                scan_id="1",
                project="TEST_PROJECT",
            )

        download_service.client.stream.assert_called_once()
        stream_path = download_service.client.stream.call_args[0][1]
        assert stream_path == "/data/experiments/MY_SESSION_LABEL/scans/1/resources/DICOM/files"

    def test_download_scans_server_error_during_resolution_falls_back_to_raw_id(
        self, download_service, tmp_path
    ):
        download_service._get = MagicMock(
            side_effect=ServerError(503, "GET", "/data/projects/TEST_PROJECT", "unavailable")
        )

        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = stream_ctx

        summary = download_service.download_scans(
            session_id="MY_SESSION_LABEL",
            scan_ids=["1"],
            output_dir=tmp_path,
            project="TEST_PROJECT",
        )

        assert summary.success is False
        download_service.client.stream.assert_called_once()
        stream_path = download_service.client.stream.call_args[0][1]
        assert stream_path == "/data/experiments/MY_SESSION_LABEL/scans/1/files"


class TestDownloadScan:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def download_service(self, mock_client):
        return DownloadService(mock_client)

    def test_download_scan_delegates_to_download_resource(self, download_service, tmp_path):
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            session_id="TEST_SESSION",
        )
        download_service.download_resource = MagicMock(return_value=mock_summary)

        result = download_service.download_scan(
            session_id="TEST_SESSION",
            scan_id="1",
            output_dir=tmp_path,
            project="TEST_PROJECT",
            resource="NIFTI",
        )

        download_service.download_resource.assert_called_once_with(
            session_id="TEST_SESSION",
            resource_label="NIFTI",
            output_dir=tmp_path,
            scan_id="1",
            project="TEST_PROJECT",
            progress_callback=None,
        )

        assert result == mock_summary


class TestDownloadScansSessionResolution:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def download_service(self, mock_client):
        return DownloadService(mock_client)

    def test_resolves_session_label_to_internal_id_resultset(self, download_service, tmp_path):
        session_label = "SESSION_LABEL"
        internal_id = "XNAT_E12345"
        project = "PROJECT"

        download_service._get = MagicMock(
            return_value={"ResultSet": {"Result": [{"ID": internal_id}]}}
        )

        mock_response = MagicMock()
        mock_response.headers = {"content-length": "0"}
        mock_response.iter_bytes.return_value = []

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(return_value=mock_response)
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = mock_stream_ctx

        download_service.download_scans(
            session_id=session_label,
            scan_ids=["6"],
            output_dir=tmp_path,
            project=project,
        )

        download_service._get.assert_called_once_with(
            f"/data/projects/{project}/experiments/{session_label}",
            params={"format": "json"},
        )

        stream_call_args = download_service.client.stream.call_args
        path_arg = stream_call_args[0][1]
        assert path_arg == f"/data/experiments/{internal_id}/scans/6/files"


class TestDownloadResourcePathConstruction:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def download_service(self, mock_client):
        return DownloadService(mock_client)

    def test_scan_resource_path_uses_experiments_endpoint(self, download_service, tmp_path):
        download_service._get = MagicMock(
            return_value={"items": [{"data_fields": {"ID": "INTERNAL_ID"}}]}
        )

        mock_response = MagicMock()
        mock_response.headers = {"content-length": "0"}
        mock_response.iter_bytes.return_value = []

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(return_value=mock_response)
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = mock_stream_ctx

        zip_path = tmp_path / "DICOM.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "content")

        with patch("builtins.open", create=True):
            with patch.object(zipfile, "ZipFile") as mock_zipfile:
                mock_zf = MagicMock()
                mock_zipfile.return_value.__enter__ = MagicMock(return_value=mock_zf)
                mock_zipfile.return_value.__exit__ = MagicMock(return_value=False)

                with pytest.raises(DownloadError):
                    download_service.download_resource(
                        session_id="SESSION_LABEL",
                        resource_label="DICOM",
                        output_dir=tmp_path,
                        scan_id="1",
                        project="PROJECT",
                    )

        stream_call_args = download_service.client.stream.call_args
        path_arg = stream_call_args[0][1]
        assert path_arg == "/data/experiments/INTERNAL_ID/scans/1/resources/DICOM/files"

    def test_session_resource_path_uses_experiments_endpoint(self, download_service, tmp_path):
        download_service._get = MagicMock(
            return_value={"items": [{"data_fields": {"ID": "INTERNAL_ID"}}]}
        )

        mock_response = MagicMock()
        mock_response.headers = {"content-length": "0"}
        mock_response.iter_bytes.return_value = []

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(return_value=mock_response)
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        download_service.client.stream.return_value = mock_stream_ctx

        with patch("builtins.open", create=True):
            with patch.object(zipfile, "ZipFile") as mock_zipfile:
                mock_zf = MagicMock()
                mock_zipfile.return_value.__enter__ = MagicMock(return_value=mock_zf)
                mock_zipfile.return_value.__exit__ = MagicMock(return_value=False)

                with pytest.raises(DownloadError):
                    download_service.download_resource(
                        session_id="SESSION_LABEL",
                        resource_label="SNAPSHOTS",
                        output_dir=tmp_path,
                        scan_id=None,
                        project="PROJECT",
                    )

        stream_call_args = download_service.client.stream.call_args
        path_arg = stream_call_args[0][1]
        assert path_arg == "/data/experiments/INTERNAL_ID/resources/SNAPSHOTS/files"


class TestDownloadResourceAndScansSkippedEntries:
    @staticmethod
    def _fake_stream_to_file(client, path, dest, *, params=None, progress_cb=None):
        from xnatctl.services.downloads import StreamedFile

        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("good.dcm", b"data")
            info = zipfile.ZipInfo("link.dcm")
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "../../../etc/passwd")
        return StreamedFile(bytes_written=dest.stat().st_size, content_length=None)

    def test_download_resource_extract_reports_skipped_entries(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(return_value=ExperimentRef(experiment="E1"))

        with (
            patch(
                "xnatctl.services.downloads.resource_scan.stream_to_file",
                side_effect=self._fake_stream_to_file,
            ),
            caplog.at_level("WARNING"),
        ):
            summary = service.download_resource(
                session_id="E1",
                resource_label="DICOM",
                output_dir=tmp_path,
                extract=True,
            )

        assert summary.success is True
        assert summary.skipped_unsafe_entries == 1
        assert (tmp_path / "DICOM" / "good.dcm").exists()
        assert not (tmp_path / "DICOM" / "link.dcm").exists()
        assert "Skipped 1 unsafe ZIP entries" in caplog.text

    def test_download_scans_extract_reports_skipped_entries(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(return_value=ExperimentRef(experiment="E1"))

        with (
            patch(
                "xnatctl.services.downloads.resource_scan.stream_to_file",
                side_effect=self._fake_stream_to_file,
            ),
            caplog.at_level("WARNING"),
        ):
            summary = service.download_scans(
                session_id="E1",
                scan_ids=["1"],
                output_dir=tmp_path,
                extract=True,
            )

        assert summary.success is True
        assert summary.skipped_unsafe_entries == 1
        assert (tmp_path / "scans" / "good.dcm").exists()
        assert not (tmp_path / "scans" / "link.dcm").exists()
        assert "Skipped 1 unsafe ZIP entries" in caplog.text


def _write_scan_zip(path: str, dest: Path) -> None:
    parts = path.strip("/").split("/")
    scan_id = parts[parts.index("scans") + 1]
    label = parts[parts.index("resources") + 1] if "resources" in parts else "DICOM"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(f"E/scans/{scan_id}/resources/{label}/files/0001.dcm", b"data")


def _write_hostile_scan_zip(path: str, dest: Path, outside: Path) -> None:
    parts = path.strip("/").split("/")
    scan_id = parts[parts.index("scans") + 1]
    label = parts[parts.index("resources") + 1] if "resources" in parts else "DICOM"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(f"E/scans/{scan_id}/resources/{label}/files/0001.dcm", b"data")
        zf.writestr(f"E/scans/{scan_id}/resources/{label}/files/linked/evil.txt", b"pwned")


class TestDownloadSessionFast:
    def _run(
        self,
        tmp_path: Path,
        *,
        scan_ids: list[str],
        stream_side_effect,
        **kwargs,
    ):
        service = DownloadService(MagicMock())
        with (
            patch(
                "xnatctl.services.downloads.SessionService.scan_rows",
                return_value=[{"ID": s} for s in scan_ids],
            ),
            patch(
                "xnatctl.services.downloads.session.stream_to_file",
                side_effect=stream_side_effect,
            ),
        ):
            return service.download_session_fast(
                session_project="P",
                subject="S",
                resolved_session_id="E",
                session_dir=tmp_path,
                workers=2,
                **kwargs,
            )

    def test_scan_only_tier_requests_one_unfiltered_zip_per_scan(self, tmp_path: Path) -> None:
        calls: list[tuple[str, dict | None]] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            calls.append((path, params))
            _write_scan_zip(path, dest)

        outcome = self._run(tmp_path, scan_ids=["1", "2"], stream_side_effect=fake_stream)

        assert sorted(calls) == [
            ("/data/projects/P/subjects/S/experiments/E/scans/1/files", {"format": "zip"}),
            ("/data/projects/P/subjects/S/experiments/E/scans/2/files", {"format": "zip"}),
        ]
        assert outcome.succeeded == 2
        assert outcome.files == 2
        assert (tmp_path / "scans" / "1" / "resources" / "DICOM" / "files" / "0001.dcm").exists()

    def test_include_filter_requests_one_zip_per_scan_resource_pair(self, tmp_path: Path) -> None:
        calls: list[tuple[str, dict | None]] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            calls.append((path, params))
            _write_scan_zip(path, dest)

        outcome = self._run(
            tmp_path,
            scan_ids=["1", "2"],
            stream_side_effect=fake_stream,
            include_resources=("DICOM", "NII"),
        )

        base = "/data/projects/P/subjects/S/experiments/E/scans"
        assert sorted(calls) == [
            (f"{base}/{sid}/resources/{label}/files", {"format": "zip"})
            for sid in ("1", "2")
            for label in ("DICOM", "NII")
        ]
        assert outcome.succeeded == 4

    def test_callbacks_fire_for_start_and_each_result_in_order(self, tmp_path: Path) -> None:
        events: list[object] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            _write_scan_zip(path, dest)

        self._run(
            tmp_path,
            scan_ids=["1", "2", "3"],
            stream_side_effect=fake_stream,
            on_start=lambda count: events.append(("start", count)),
            on_scan_result=lambda r: events.append(r),
        )

        assert events[0] == ("start", 3)
        results = events[1:]
        assert len(results) == 3
        assert all(isinstance(r, ScanResult) and r.ok for r in results)

    def test_no_scans_reports_zero_and_returns_empty_outcome(self, tmp_path: Path) -> None:
        started: list[int] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            raise AssertionError("no scan should be requested")

        outcome = self._run(
            tmp_path,
            scan_ids=[],
            stream_side_effect=fake_stream,
            on_start=started.append,
        )

        assert started == [0]
        assert outcome == DownloadOutcome(succeeded=0, failed=[], files=0)

    def test_all_404_scans_are_not_failures_but_yield_zero_files(self, tmp_path: Path) -> None:
        def fake_stream(client, path, dest, *, params=None, **kw):
            raise ResourceNotFoundError("no files", path)

        outcome = self._run(tmp_path, scan_ids=["1", "2"], stream_side_effect=fake_stream)

        assert outcome.succeeded == 2
        assert outcome.failed == []
        assert outcome.files == 0

    def test_a_hard_failure_is_recorded_in_the_outcome(self, tmp_path: Path) -> None:
        results: list[ScanResult] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            if "/scans/2/" in path:
                raise RuntimeError("upstream exploded")
            _write_scan_zip(path, dest)

        outcome = self._run(
            tmp_path,
            scan_ids=["1", "2"],
            stream_side_effect=fake_stream,
            on_scan_result=results.append,
        )

        assert outcome.succeeded == 1
        assert [scan for scan, _msg in outcome.failed] == ["2"]
        assert any(not r.ok and r.scan_id == "2" for r in results)

    def test_hostile_zip_entries_are_counted_reported_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        outside = tmp_path.parent / "outside_download_session_fast"
        outside.mkdir(exist_ok=True)

        def fake_stream(client, path, dest, *, params=None, **kw):
            scan_id = path.strip("/").split("/")[path.strip("/").split("/").index("scans") + 1]
            files_dir = tmp_path / "scans" / scan_id / "resources" / "DICOM" / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            link = files_dir / "linked"
            if not link.exists():
                link.symlink_to(outside, target_is_directory=True)
            _write_hostile_scan_zip(path, dest, outside)

        with caplog.at_level("WARNING"):
            outcome = self._run(tmp_path, scan_ids=["1"], stream_side_effect=fake_stream)

        assert outcome.succeeded == 1
        assert outcome.files == 1
        assert outcome.skipped_unsafe_entries == 1
        assert not (outside / "evil.txt").exists()
        assert "Skipped 1 unsafe ZIP entries" in caplog.text

    def test_shared_scans_dir_exists_before_any_worker_starts(self, tmp_path: Path) -> None:
        seen_before_stream: list[bool] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            seen_before_stream.append((tmp_path / "scans").exists())
            _write_scan_zip(path, dest)

        outcome = self._run(tmp_path, scan_ids=["1", "2", "3"], stream_side_effect=fake_stream)

        assert outcome.succeeded == 3
        assert seen_before_stream == [True, True, True]


class TestDownloadSessionArchiveAndResources:
    def test_archive_streams_scans_zip_and_forwards_progress(self, tmp_path: Path) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def fake_stream(client, path, dest, *, params=None, progress_cb=None, **kw):
            calls.append((path, str(dest), params))
            if progress_cb is not None:
                progress_cb(10, 10)
            dest.write_bytes(b"zip")

        seen: list[tuple[int, int | None]] = []
        with patch("xnatctl.services.downloads.session.stream_to_file", side_effect=fake_stream):
            out = DownloadService(MagicMock()).download_session_archive(
                session_project="P",
                subject="S",
                resolved_session_id="E",
                session_dir=tmp_path,
                progress_cb=lambda w, t: seen.append((w, t)),
            )

        assert out == tmp_path / "scans.zip"
        assert calls == [
            (
                "/data/projects/P/subjects/S/experiments/E/scans/ALL/files",
                str(tmp_path / "scans.zip"),
                {"format": "zip"},
            )
        ]
        assert seen == [(10, 10)]

    def test_resources_stream_one_zip_per_resource_and_return_count(self, tmp_path: Path) -> None:
        urls: list[tuple[str, dict | None]] = []

        def fake_stream(client, path, dest, *, params=None, **kw):
            urls.append((path, params))
            dest.write_bytes(b"zip")

        with (
            patch(
                "xnatctl.services.downloads.SessionService.experiment_resource_rows",
                return_value=[{"label": "QC"}, {"label": "MISC"}],
            ) as rows_mock,
            patch("xnatctl.services.downloads.session.stream_to_file", side_effect=fake_stream),
        ):
            downloaded = DownloadService(MagicMock()).download_session_level_resources(
                session_project="P",
                subject="S",
                resolved_session_id="E",
                session_dir=tmp_path,
            )

        assert downloaded == [
            ("QC", tmp_path / "resources_QC.zip"),
            ("MISC", tmp_path / "resources_MISC.zip"),
        ]
        rows_mock.assert_called_once_with("E", project="P", subject="S")
        assert urls == [
            ("/data/projects/P/subjects/S/experiments/E/resources/QC/files", {"format": "zip"}),
            ("/data/projects/P/subjects/S/experiments/E/resources/MISC/files", {"format": "zip"}),
        ]
        assert (tmp_path / "resources_QC.zip").exists()
        assert (tmp_path / "resources_MISC.zip").exists()

    def test_provenance_survives_a_mid_loop_failure_via_the_downloaded_list(
        self, tmp_path: Path
    ) -> None:
        def fake_stream(client, path, dest, *, params=None, **kw):
            if "MISC" in path:
                raise DownloadError("boom", path)
            dest.write_bytes(b"zip")

        caller_list: list[tuple[str, Path]] = []
        with (
            patch(
                "xnatctl.services.downloads.SessionService.experiment_resource_rows",
                return_value=[{"label": "QC"}, {"label": "MISC"}],
            ),
            patch("xnatctl.services.downloads.session.stream_to_file", side_effect=fake_stream),
        ):
            with pytest.raises(DownloadError):
                DownloadService(MagicMock()).download_session_level_resources(
                    session_project="P",
                    subject="S",
                    resolved_session_id="E",
                    session_dir=tmp_path,
                    downloaded=caller_list,
                )

        assert caller_list == [("QC", tmp_path / "resources_QC.zip")]


class TestSingleTargetDownloadContract:
    def _service(self) -> DownloadService:
        return DownloadService(MagicMock())

    def test_session_expired_propagates_unwrapped(self, tmp_path: Path) -> None:
        service = self._service()
        with patch(
            "xnatctl.services.downloads.resource_scan.stream_to_file",
            side_effect=SessionExpiredError("https://xnat.example.org"),
        ):
            try:
                service.download_resource(
                    session_id="XNAT_E00001",
                    resource_label="DICOM",
                    output_dir=tmp_path,
                )
            except SessionExpiredError:
                pass
            else:
                raise AssertionError("SessionExpiredError did not propagate")

    def test_disk_full_oserror_surfaces_as_download_error_with_cause(self, tmp_path: Path) -> None:
        service = self._service()
        disk_full = OSError("No space left on device")
        with patch(
            "xnatctl.services.downloads.resource_scan.stream_to_file",
            side_effect=disk_full,
        ):
            with pytest.raises(DownloadError) as excinfo:
                service.download_resource(
                    session_id="XNAT_E00001",
                    resource_label="DICOM",
                    output_dir=tmp_path,
                )
        assert excinfo.value.__cause__ is disk_full
        assert "No space left on device" in str(excinfo.value)
        assert excinfo.value.resource == "DICOM"

    def test_typed_failure_is_the_same_exception_object(self, tmp_path: Path) -> None:
        service = self._service()
        exc = SessionExpiredError("https://xnat.example.org")
        with patch("xnatctl.services.downloads.resource_scan.stream_to_file", side_effect=exc):
            with pytest.raises(SessionExpiredError) as excinfo:
                service.download_resource(
                    session_id="XNAT_E00001",
                    resource_label="DICOM",
                    output_dir=tmp_path,
                )
        assert excinfo.value is exc

    def test_error_progress_fires_and_a_raising_callback_cannot_mask(self, tmp_path: Path) -> None:
        service = self._service()
        phases: list[OperationPhase] = []

        def callback(progress: DownloadProgress) -> None:
            phases.append(progress.phase)
            if progress.phase is OperationPhase.ERROR:
                raise RuntimeError("callback exploded")

        exc = SessionExpiredError("https://xnat.example.org")
        with patch("xnatctl.services.downloads.resource_scan.stream_to_file", side_effect=exc):
            with pytest.raises(SessionExpiredError) as excinfo:
                service.download_resource(
                    session_id="XNAT_E00001",
                    resource_label="DICOM",
                    output_dir=tmp_path,
                    progress_callback=callback,
                )
        assert excinfo.value is exc
        assert OperationPhase.ERROR in phases


class TestDownloadScanDelegation:
    def _service(self) -> DownloadService:
        return DownloadService(MagicMock())

    def test_resource_path_raises(self, tmp_path: Path) -> None:
        service = self._service()
        exc = SessionExpiredError("https://xnat.example.org")
        with patch("xnatctl.services.downloads.resource_scan.stream_to_file", side_effect=exc):
            with pytest.raises(SessionExpiredError):
                service.download_scan(
                    session_id="XNAT_E00001",
                    scan_id="2",
                    output_dir=tmp_path,
                    resource="DICOM",
                )

    def test_batch_path_returns_the_batch_summary_unraised(self, tmp_path: Path) -> None:
        service = self._service()
        failed = DownloadSummary(
            success=False, total=1, succeeded=0, failed=1, duration=0.1, errors=["boom"]
        )
        with patch.object(service, "download_scans", return_value=failed) as scans:
            result = service.download_scan(
                session_id="XNAT_E00001",
                scan_id="2",
                output_dir=tmp_path,
            )
        assert result is failed
        scans.assert_called_once()


class TestBuildVerificationManifest:
    def _service(self) -> DownloadService:
        return DownloadService(MagicMock())

    def test_keys_by_scan_and_resource_not_basename(self, tmp_path: Path) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.SessionService.scan_rows",
                return_value=[{"ID": "1"}, {"ID": "2"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch("xnatctl.services.downloads.ResourceService.list_file_rows") as file_rows_mock,
        ):

            def fake_file_rows(scan_ref, label):
                scan_id = scan_ref.scan_id
                return [
                    {
                        "Name": "1.dcm",
                        "URI": f"/data/experiments/E/scans/{scan_id}/resources/DICOM/files/1.dcm",
                        "digest": f"digest-{scan_id}",
                    }
                ]

            file_rows_mock.side_effect = fake_file_rows

            manifest = service.build_verification_manifest(session_id="E", project="P", subject="S")

        assert manifest.digests == {
            "scans/1/resources/DICOM/1.dcm": "digest-1",
            "scans/2/resources/DICOM/1.dcm": "digest-2",
        }
        assert manifest.collisions == []

    def test_missing_digest_maps_to_none(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {
                        "Name": "1.dcm",
                        "URI": "/data/experiments/E/scans/1/resources/DICOM/files/1.dcm",
                    }
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert manifest.digests == {"scans/1/resources/DICOM/1.dcm": None}

    def test_name_fallback_key_agrees_with_uri_derived_key_for_a_scan_resource(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[{"Name": "1.dcm"}],
            ),
        ):
            fallback_manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {
                        "Name": "1.dcm",
                        "URI": "/data/experiments/E/scans/1/resources/DICOM/files/1.dcm",
                    }
                ],
            ),
        ):
            uri_manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert fallback_manifest.digests.keys() == uri_manifest.digests.keys()
        assert fallback_manifest.digests.keys() == {"scans/1/resources/DICOM/1.dcm"}

    def test_name_fallback_key_agrees_with_uri_derived_key_for_a_session_resource(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.SessionService.experiment_resource_rows",
                return_value=[{"label": "QC"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[{"Name": "notes.txt"}],
            ),
        ):
            fallback_manifest = service.build_verification_manifest(
                session_id="E",
                project="P",
                subject="S",
                scan_ids=[],
                include_session_resources=True,
            )

        assert fallback_manifest.digests.keys() == {"resources/QC/notes.txt"}

    def test_include_resources_filters_the_scan_resource_listing(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}, {"label": "SNAPSHOTS"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {
                        "Name": "1.dcm",
                        "URI": "/data/experiments/E/scans/1/resources/DICOM/files/1.dcm",
                        "digest": "abc",
                    }
                ],
            ) as file_rows_mock,
        ):
            manifest = service.build_verification_manifest(
                session_id="E",
                project="P",
                subject="S",
                scan_ids=["1"],
                include_resources=("DICOM",),
            )

        assert manifest.digests == {"scans/1/resources/DICOM/1.dcm": "abc"}
        file_rows_mock.assert_called_once()

    def test_resource_filter_skips_the_resource_listing_call(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch("xnatctl.services.downloads.ResourceService.list_rows") as list_rows_mock,
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {
                        "Name": "1.dcm",
                        "URI": "/data/experiments/E/scans/1/resources/DICOM/files/1.dcm",
                        "digest": "abc",
                    }
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E",
                project="P",
                subject="S",
                scan_ids=["1"],
                resource_filter="DICOM",
            )

        assert manifest.digests == {"scans/1/resources/DICOM/1.dcm": "abc"}
        list_rows_mock.assert_not_called()

    def test_label_is_passed_through_for_the_request_but_literal_in_the_key(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "QA #1"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {
                        "Name": "1.dcm",
                        # XNAT's JSON URI field is literal text, not
                        # URL-encoded -- matches what a local/ZIP path would
                        # contain for the same label.
                        "URI": "/data/experiments/E/scans/1/resources/QA #1/files/1.dcm",
                        "digest": "abc",
                    }
                ],
            ) as file_rows_mock,
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert manifest.digests == {"scans/1/resources/QA #1/1.dcm": "abc"}
        call_args = file_rows_mock.call_args
        assert call_args[0][1] == "QA #1"

    def test_session_resources_are_included_when_requested(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.SessionService.experiment_resource_rows",
                return_value=[{"label": "QC"}],
            ) as session_rows_mock,
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {
                        "Name": "notes.txt",
                        "URI": "/data/experiments/E/resources/QC/files/notes.txt",
                        "digest": "abc",
                    }
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E",
                project="P",
                subject="S",
                scan_ids=[],
                include_session_resources=True,
            )

        assert manifest.digests == {"resources/QC/notes.txt": "abc"}
        session_rows_mock.assert_called_once_with("E", project="P", subject="S")

    def test_session_resources_are_skipped_by_default(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.SessionService.experiment_resource_rows"
            ) as session_rows_mock,
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=[]
            )

        assert manifest.digests == {}
        session_rows_mock.assert_not_called()

    def test_two_rows_with_different_digests_on_the_same_key_are_a_collision(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {"Name": "1.dcm", "digest": "aaa"},
                    {"Name": "1.dcm", "digest": "bbb"},
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert manifest.digests == {}
        assert manifest.collisions == ["scans/1/resources/DICOM/1.dcm"]

    def test_identical_repeated_row_is_not_a_collision(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {"Name": "1.dcm", "digest": "aaa"},
                    {"Name": "1.dcm", "digest": "aaa"},
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert manifest.digests == {"scans/1/resources/DICOM/1.dcm": "aaa"}
        assert manifest.collisions == []

    def test_case_differing_digests_are_not_a_collision(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {"Name": "1.dcm", "digest": "AAA111"},
                    {"Name": "1.dcm", "digest": "aaa111"},
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert manifest.digests.keys() == {"scans/1/resources/DICOM/1.dcm"}
        digest = manifest.digests["scans/1/resources/DICOM/1.dcm"]
        assert digest is not None and digest.lower() == "aaa111"
        assert manifest.collisions == []

    def test_a_collided_key_stays_collided_even_if_a_later_row_matches_the_first(
        self,
    ) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_resolve_zip_experiment_ref",
                return_value=ExperimentRef(experiment="E"),
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_rows",
                return_value=[{"label": "DICOM"}],
            ),
            patch(
                "xnatctl.services.downloads.ResourceService.list_file_rows",
                return_value=[
                    {"Name": "1.dcm", "digest": "aaa"},
                    {"Name": "1.dcm", "digest": "bbb"},
                    {"Name": "1.dcm", "digest": "aaa"},
                ],
            ),
        ):
            manifest = service.build_verification_manifest(
                session_id="E", project="P", subject="S", scan_ids=["1"]
            )

        assert manifest.digests == {}
        assert manifest.collisions == ["scans/1/resources/DICOM/1.dcm"]


class TestVerifyScanDownloads:
    def test_delegates_to_manifest_and_verify_manifest(self, tmp_path: Path) -> None:
        service = DownloadService(MagicMock())
        target = tmp_path / "scans" / "1" / "resources" / "DICOM" / "files" / "1.dcm"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"data")

        with patch.object(
            service,
            "build_verification_manifest",
            return_value=VerificationManifest(
                digests={"scans/1/resources/DICOM/1.dcm": hashlib.md5(b"data").hexdigest()},
                collisions=[],
            ),
        ) as manifest_mock:
            report = service.verify_scan_downloads(
                session_id="E",
                project="P",
                subject="S",
                scan_ids=["1"],
                local_root=tmp_path,
            )

        manifest_mock.assert_called_once_with(
            session_id="E",
            project="P",
            subject="S",
            scan_ids=["1"],
            include_resources=(),
            exclude_resources=(),
            resource_filter=None,
            include_session_resources=False,
        )
        assert report.success is True
        assert report.matched == 1

    def test_server_side_collisions_carry_through_to_the_report(self, tmp_path: Path) -> None:
        service = DownloadService(MagicMock())
        with patch.object(
            service,
            "build_verification_manifest",
            return_value=VerificationManifest(
                digests={}, collisions=["scans/1/resources/DICOM/1.dcm"]
            ),
        ):
            report = service.verify_scan_downloads(
                session_id="E", project="P", subject="S", local_root=tmp_path
            )

        assert report.collisions == ["scans/1/resources/DICOM/1.dcm"]
        assert report.success is False


class TestDownloadSummaryRaiseForStatus:
    def test_raises_batch_operation_error_on_failure(self) -> None:
        summary = DownloadSummary(
            success=False,
            total=3,
            succeeded=1,
            failed=2,
            duration=1.0,
            errors=["scan 2 exploded", "scan 3 exploded"],
        )

        with pytest.raises(BatchOperationError) as excinfo:
            summary.raise_for_status()

        err = excinfo.value
        assert err.succeeded == 1
        assert err.failed == 2
        assert err.errors == ["scan 2 exploded", "scan 3 exploded"]
        assert err.details["succeeded"] == 1
        assert err.details["failed"] == 2
        assert "download" in str(err)

    def test_noop_on_success(self) -> None:
        summary = DownloadSummary(
            success=True,
            total=2,
            succeeded=2,
            failed=0,
            duration=1.0,
        )
        assert summary.raise_for_status() is None
