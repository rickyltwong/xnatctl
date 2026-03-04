"""Tests for transfer executor."""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from xnatctl.services.transfer.executor import TransferExecutor


def _make_response(
    json_data: dict | str | None = None,
    status_code: int = 200,
    text: str = "",
) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    resp.text = text or str(json_data or "")
    resp.headers = {"content-type": "application/json"}
    return resp


def _make_valid_zip() -> bytes:
    """Create minimal valid ZIP bytes in memory."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.dcm", "fake dicom data")
    return buf.getvalue()


def _make_nested_zip(
    experiment_label: str = "EXP001",
    scan_id: str = "4",
    resource_label: str = "SNAPSHOTS",
    filenames: list[str] | None = None,
) -> bytes:
    """Create a ZIP with XNAT's nested directory structure.

    XNAT serves ZIPs with paths like:
    ``experiment_label/scans/scan_id/resources/label/files/filename``

    Args:
        experiment_label: Experiment label for directory prefix.
        scan_id: Scan ID for directory prefix.
        resource_label: Resource label for directory prefix.
        filenames: Leaf filenames to include.

    Returns:
        ZIP bytes with nested directory hierarchy.
    """
    import io

    if filenames is None:
        filenames = ["image_qc.gif", "montage_2x3.gif"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        prefix = f"{experiment_label}/scans/{scan_id}/resources/{resource_label}/files"
        for name in filenames:
            zf.writestr(f"{prefix}/{name}", f"data for {name}")
    return buf.getvalue()


def _mock_stream_download(source_client: MagicMock, data: bytes) -> None:
    """Configure source_client to return data from streaming download."""
    stream_ctx = MagicMock()
    stream_resp = MagicMock()
    stream_resp.headers = {"content-length": str(len(data))}
    stream_resp.iter_bytes.return_value = [data]
    stream_resp.raise_for_status = MagicMock()
    stream_ctx.__enter__ = MagicMock(return_value=stream_resp)
    stream_ctx.__exit__ = MagicMock(return_value=False)

    inner_client = MagicMock()
    inner_client.stream.return_value = stream_ctx
    source_client._get_client.return_value = inner_client
    source_client._get_cookies.return_value = {}


@pytest.fixture
def source_client() -> MagicMock:
    """Mock XNATClient for source server."""
    client = MagicMock()
    client.base_url = "https://src.example.org"
    return client


@pytest.fixture
def dest_client() -> MagicMock:
    """Mock XNATClient for destination server."""
    client = MagicMock()
    client.base_url = "https://dst.example.org"
    return client


@pytest.fixture
def executor(source_client: MagicMock, dest_client: MagicMock) -> TransferExecutor:
    """TransferExecutor wired to mock clients."""
    return TransferExecutor(source_client, dest_client)


class TestCreateSubject:
    def test_create_subject_puts_to_dest(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.put.return_value = _make_response(text="/data/subjects/XNAT_S999")
        result = executor.create_subject("DST", "SUB001")
        dest_client.put.assert_called_once()
        assert "/data/subjects/XNAT_S999" == result


class TestCreateExperiment:
    def test_create_experiment_puts_with_xsi_type(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.put.return_value = _make_response(text="/data/experiments/XNAT_E001")
        result = executor.create_experiment("DST", "SUB001", "EXP001", "xnat:mrSessionData")
        dest_client.put.assert_called_once()
        call_args = dest_client.put.call_args
        assert "DST" in call_args[0][0]
        assert "SUB001" in call_args[0][0]
        assert "EXP001" in call_args[0][0]
        assert call_args[1]["params"]["xsiType"] == "xnat:mrSessionData"
        assert result == "/data/experiments/XNAT_E001"


class TestCreateScan:
    def test_create_scan_puts_with_type(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.put.return_value = _make_response(text="")
        executor.create_scan("DST", "SUB001", "EXP001", "22", "TEAvg_se")
        dest_client.put.assert_called_once()
        call_args = dest_client.put.call_args
        assert "/scans/22" in call_args[0][0]
        assert call_args[1]["params"]["type"] == "TEAvg_se"
        assert call_args[1]["params"]["xsiType"] == "xnat:mrScanData"


class TestCheckExperimentExists:
    def test_returns_id_when_found(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.get.return_value = _make_response(
            json_data={"ResultSet": {"Result": [{"ID": "XNAT_E001", "label": "EXP001"}]}}
        )
        result = executor.check_experiment_exists("DST", "EXP001")
        assert result == "XNAT_E001"

    def test_returns_none_when_not_found(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.get.return_value = _make_response(json_data={"ResultSet": {"Result": []}})
        result = executor.check_experiment_exists("DST", "EXP001")
        assert result is None


class TestDiscoverScans:
    def test_returns_scan_list(self, executor: TransferExecutor, source_client: MagicMock) -> None:
        source_client.get.return_value = _make_response(
            json_data={
                "ResultSet": {
                    "Result": [
                        {"ID": "1", "type": "T1w"},
                        {"ID": "2", "type": "fMRI"},
                    ]
                }
            }
        )
        scans = executor.discover_scans("XNAT_E001")
        assert len(scans) == 2
        assert scans[0]["ID"] == "1"


class TestDiscoverScanResources:
    def test_returns_resource_list(
        self, executor: TransferExecutor, source_client: MagicMock
    ) -> None:
        source_client.get.return_value = _make_response(
            json_data={
                "ResultSet": {
                    "Result": [
                        {"label": "DICOM", "file_count": "100"},
                        {"label": "SNAPSHOTS", "file_count": "2"},
                    ]
                }
            }
        )
        resources = executor.discover_scan_resources("XNAT_E001", "1")
        assert len(resources) == 2
        assert resources[0]["label"] == "DICOM"


class TestDiscoverSessionResources:
    def test_returns_session_resource_list(
        self, executor: TransferExecutor, source_client: MagicMock
    ) -> None:
        source_client.get.return_value = _make_response(
            json_data={"ResultSet": {"Result": [{"label": "QC", "file_count": "1"}]}}
        )
        resources = executor.discover_session_resources("XNAT_E001")
        assert len(resources) == 1
        assert resources[0]["label"] == "QC"


class TestTransferScanDicom:
    def test_downloads_validates_and_imports(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        zip_data = _make_valid_zip()
        _mock_stream_download(source_client, zip_data)
        dest_client.post.return_value = _make_response(text="/data/experiments/E999")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = executor.transfer_scan_dicom(
                source_experiment_id="XNAT_E001",
                scan_id="1",
                dest_project="DST",
                dest_subject="SUB001",
                dest_experiment_label="EXP001",
                work_dir=Path(tmpdir),
            )
        assert result == "/data/experiments/E999"
        dest_client.post.assert_called_once()

    def test_retries_on_import_failure(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        zip_data = _make_valid_zip()
        _mock_stream_download(source_client, zip_data)

        # First import fails, second succeeds
        dest_client.post.side_effect = [
            RuntimeError("import failed"),
            _make_response(text="/data/experiments/E999"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = executor.transfer_scan_dicom(
                source_experiment_id="XNAT_E001",
                scan_id="1",
                dest_project="DST",
                dest_subject="SUB001",
                dest_experiment_label="EXP001",
                work_dir=Path(tmpdir),
                retry_count=2,
                retry_delay=0.01,
            )
        assert result == "/data/experiments/E999"
        assert dest_client.post.call_count == 2

    def test_retains_zip_on_final_failure(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        zip_data = _make_valid_zip()
        _mock_stream_download(source_client, zip_data)
        dest_client.post.side_effect = RuntimeError("permanent failure")

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(RuntimeError, match="permanent failure"):
                executor.transfer_scan_dicom(
                    source_experiment_id="XNAT_E001",
                    scan_id="1",
                    dest_project="DST",
                    dest_subject="SUB001",
                    dest_experiment_label="EXP001",
                    work_dir=Path(tmpdir),
                    retry_count=1,
                    retry_delay=0.01,
                )
            # ZIP should be retained for debugging
            assert (Path(tmpdir) / "scan_1_DICOM.zip").exists()

    def test_raises_on_invalid_zip(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
    ) -> None:
        # Download non-ZIP data
        _mock_stream_download(source_client, b"not a zip file at all")

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="ZIP validation failed"):
                executor.transfer_scan_dicom(
                    source_experiment_id="XNAT_E001",
                    scan_id="1",
                    dest_project="DST",
                    dest_subject="SUB001",
                    dest_experiment_label="EXP001",
                    work_dir=Path(tmpdir),
                )


class TestTransferResource:
    def test_downloads_and_uploads(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        zip_data = _make_valid_zip()
        _mock_stream_download(source_client, zip_data)
        dest_client.put.return_value = _make_response(text="OK")

        with tempfile.TemporaryDirectory() as tmpdir:
            total = executor.transfer_resource(
                source_path="/data/experiments/E001/resources/NII/files",
                dest_path="/data/experiments/E002/resources/NII/files",
                resource_label="NII",
                work_dir=Path(tmpdir),
            )
        assert total == len(zip_data)
        dest_client.put.assert_called_once()

    def test_cleans_up_zip_after_success(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        zip_data = _make_valid_zip()
        _mock_stream_download(source_client, zip_data)
        dest_client.put.return_value = _make_response(text="OK")

        with tempfile.TemporaryDirectory() as tmpdir:
            executor.transfer_resource(
                source_path="/src/path",
                dest_path="/dst/path",
                resource_label="NII",
                work_dir=Path(tmpdir),
            )
            assert not (Path(tmpdir) / "NII.zip").exists()
            assert not (Path(tmpdir) / "NII_flat.zip").exists()

    def test_uploads_flat_zip_from_nested_source(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Nested XNAT ZIP hierarchy is flattened before upload."""
        nested_zip = _make_nested_zip(
            experiment_label="SRC_EXP",
            scan_id="4",
            resource_label="SNAPSHOTS",
            filenames=["qc_image.gif", "montage.gif"],
        )
        _mock_stream_download(source_client, nested_zip)
        dest_client.put.return_value = _make_response(text="OK")

        with tempfile.TemporaryDirectory() as tmpdir:
            executor.transfer_resource(
                source_path="/data/experiments/E001/scans/4/resources/SNAPSHOTS/files",
                dest_path="/data/experiments/E002/scans/4/resources/SNAPSHOTS/files",
                resource_label="SNAPSHOTS",
                work_dir=Path(tmpdir),
            )

        # Verify the uploaded ZIP contains only flat filenames
        call_args = dest_client.put.call_args
        uploaded_data = call_args[1]["data"]

        import io

        with zipfile.ZipFile(io.BytesIO(uploaded_data), "r") as zf:
            names = zf.namelist()
        assert sorted(names) == ["montage.gif", "qc_image.gif"]

    def test_raises_on_invalid_zip(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
    ) -> None:
        _mock_stream_download(source_client, b"not a zip")

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="ZIP validation failed"):
                executor.transfer_resource(
                    source_path="/src/path",
                    dest_path="/dst/path",
                    resource_label="BAD",
                    work_dir=Path(tmpdir),
                )


class TestFlattenZip:
    def test_strips_xnat_prefix(self, tmp_path: Path) -> None:
        """XNAT prefix up to files/ is stripped, leaf filenames preserved."""
        src = tmp_path / "nested.zip"
        dst = tmp_path / "flat.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("EXP/scans/1/resources/SNAP/files/a.gif", "aaa")
            zf.writestr("EXP/scans/1/resources/SNAP/files/b.gif", "bbb")

        TransferExecutor._flatten_zip(src, dst)

        with zipfile.ZipFile(dst, "r") as zf:
            assert sorted(zf.namelist()) == ["a.gif", "b.gif"]
            assert zf.read("a.gif") == b"aaa"
            assert zf.read("b.gif") == b"bbb"

    def test_preserves_subdirs_under_files(self, tmp_path: Path) -> None:
        """Subdirectory structure within files/ is preserved."""
        src = tmp_path / "nested.zip"
        dst = tmp_path / "flat.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("EXP/scans/1/resources/BIDS/files/sub-01/anat/T1w.nii", "data")
            zf.writestr("EXP/scans/1/resources/BIDS/files/dataset_description.json", "{}")

        TransferExecutor._flatten_zip(src, dst)

        with zipfile.ZipFile(dst, "r") as zf:
            assert sorted(zf.namelist()) == ["dataset_description.json", "sub-01/anat/T1w.nii"]

    def test_skips_directory_entries(self, tmp_path: Path) -> None:
        """Directory-only entries in the ZIP are excluded."""
        src = tmp_path / "nested.zip"
        dst = tmp_path / "flat.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("dir/files/", "")
            zf.writestr("dir/files/file.txt", "content")

        TransferExecutor._flatten_zip(src, dst)

        with zipfile.ZipFile(dst, "r") as zf:
            assert zf.namelist() == ["file.txt"]

    def test_raises_on_duplicate_paths(self, tmp_path: Path) -> None:
        """Duplicate relative paths from different prefixes raise ValueError."""
        src = tmp_path / "dup.zip"
        dst = tmp_path / "flat.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("path_a/files/qc.gif", "aaa")
            zf.writestr("path_b/files/qc.gif", "bbb")

        with pytest.raises(ValueError, match="Duplicate path 'qc.gif'"):
            TransferExecutor._flatten_zip(src, dst)

    def test_already_flat_zip_unchanged(self, tmp_path: Path) -> None:
        """ZIP with no directory structure passes through unchanged."""
        src = tmp_path / "flat_in.zip"
        dst = tmp_path / "flat_out.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("file1.dcm", "data1")
            zf.writestr("file2.dcm", "data2")

        TransferExecutor._flatten_zip(src, dst)

        with zipfile.ZipFile(dst, "r") as zf:
            assert sorted(zf.namelist()) == ["file1.dcm", "file2.dcm"]


class TestValidateZip:
    def test_valid_zip_passes(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(_make_valid_zip())
        assert TransferExecutor.validate_zip(zip_path) is True

    def test_nonexistent_file_fails(self, tmp_path: Path) -> None:
        assert TransferExecutor.validate_zip(tmp_path / "nope.zip") is False

    def test_non_zip_file_fails(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip at all")
        assert TransferExecutor.validate_zip(bad) is False

    def test_content_length_mismatch_fails(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "test.zip"
        data = _make_valid_zip()
        zip_path.write_bytes(data)
        assert TransferExecutor.validate_zip(zip_path, expected_size=999) is False

    def test_content_length_match_passes(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "test.zip"
        data = _make_valid_zip()
        zip_path.write_bytes(data)
        assert TransferExecutor.validate_zip(zip_path, expected_size=len(data)) is True


class TestFindPrearchiveEntry:
    def test_returns_matching_entry(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.get.return_value = _make_response(
            json_data={
                "ResultSet": {
                    "Result": [
                        {"name": "EXP001", "status": "READY", "timestamp": "20260101_100000"},
                        {"name": "EXP002", "status": "RECEIVING", "timestamp": "20260101_110000"},
                    ]
                }
            }
        )
        entry = executor.find_prearchive_entry("DST", "EXP001")
        assert entry is not None
        assert entry["status"] == "READY"

    def test_returns_none_when_not_found(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.get.return_value = _make_response(json_data={"ResultSet": {"Result": []}})
        assert executor.find_prearchive_entry("DST", "EXP001") is None

    def test_matches_folder_name(self, executor: TransferExecutor, dest_client: MagicMock) -> None:
        dest_client.get.return_value = _make_response(
            json_data={
                "ResultSet": {
                    "Result": [
                        {"name": "other", "folderName": "EXP001", "status": "READY"},
                    ]
                }
            }
        )
        entry = executor.find_prearchive_entry("DST", "EXP001")
        assert entry is not None


class TestArchivePrearchive:
    def test_posts_commit_action(self, executor: TransferExecutor, dest_client: MagicMock) -> None:
        dest_client.post.return_value = _make_response(text="OK")
        executor.archive_prearchive(
            dest_project="DST",
            timestamp="20260101_100000",
            session_name="EXP001",
            subject_label="SUB001",
            experiment_label="EXP001",
        )
        dest_client.post.assert_called_once()
        call_args = dest_client.post.call_args
        assert "20260101_100000" in call_args[0][0]
        assert call_args[1]["params"]["action"] == "commit"
        assert call_args[1]["params"]["subject"] == "SUB001"
        assert call_args[1]["params"]["label"] == "EXP001"

    def test_posts_overwrite_when_specified(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.post.return_value = _make_response(text="OK")
        executor.archive_prearchive(
            dest_project="DST",
            timestamp="20260101_100000",
            session_name="EXP001",
            subject_label="SUB001",
            experiment_label="EXP001",
            overwrite="append",
        )
        call_args = dest_client.post.call_args
        assert call_args[1]["params"]["overwrite"] == "append"


class TestCountDestScans:
    def test_returns_scan_count(self, executor: TransferExecutor, dest_client: MagicMock) -> None:
        dest_client.get.return_value = _make_response(
            json_data={
                "ResultSet": {
                    "Result": [
                        {"ID": "1", "type": "T1w"},
                        {"ID": "2", "type": "fMRI"},
                        {"ID": "3", "type": "DWI"},
                    ]
                }
            }
        )
        count = executor.count_dest_scans("DST", "SUB001", "EXP001")
        assert count == 3

    def test_returns_zero_when_empty(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.get.return_value = _make_response(json_data={"ResultSet": {"Result": []}})
        assert executor.count_dest_scans("DST", "SUB001", "EXP001") == 0


class TestWaitForArchive:
    @patch("xnatctl.services.transfer.executor.time.sleep")
    def test_returns_immediately_when_scans_present(
        self,
        mock_sleep: MagicMock,
        executor: TransferExecutor,
        dest_client: MagicMock,
    ) -> None:
        """No prearchive entry + scans already in archive -> immediate return."""
        # find_prearchive_entry returns None (no entry)
        # count_dest_scans returns 5
        dest_client.get.side_effect = [
            _make_response(json_data={"ResultSet": {"Result": []}}),  # prearchive
            _make_response(  # scan count
                json_data={"ResultSet": {"Result": [{"ID": str(i)} for i in range(5)]}}
            ),
        ]
        actual = executor.wait_for_archive("DST", "SUB001", "EXP001", 5)
        assert actual == 5
        mock_sleep.assert_not_called()

    @patch("xnatctl.services.transfer.executor.time.sleep")
    def test_archives_ready_entry_then_returns(
        self,
        mock_sleep: MagicMock,
        executor: TransferExecutor,
        dest_client: MagicMock,
    ) -> None:
        """Prearchive entry READY -> archive it -> next poll finds scans."""
        dest_client.get.side_effect = [
            # Poll 1: find_prearchive_entry -> READY
            _make_response(
                json_data={
                    "ResultSet": {
                        "Result": [
                            {"name": "EXP001", "status": "READY", "timestamp": "20260101_100000"}
                        ]
                    }
                }
            ),
            # Poll 2: find_prearchive_entry -> empty (archived)
            _make_response(json_data={"ResultSet": {"Result": []}}),
            # Poll 2: count_dest_scans -> 3
            _make_response(json_data={"ResultSet": {"Result": [{"ID": str(i)} for i in range(3)]}}),
        ]
        dest_client.post.return_value = _make_response(text="OK")

        actual = executor.wait_for_archive("DST", "SUB001", "EXP001", 3, timeout=60, interval=0.01)
        assert actual == 3
        dest_client.post.assert_called_once()  # archive_prearchive called

    @patch("xnatctl.services.transfer.executor.time.monotonic")
    @patch("xnatctl.services.transfer.executor.time.sleep")
    def test_timeout_returns_partial_count(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        executor: TransferExecutor,
        dest_client: MagicMock,
    ) -> None:
        """Timeout exceeded -> return whatever count is available."""
        # monotonic calls: (1) deadline=0+10=10, (2) check=5 < 10 -> not timed out,
        # (3) deadline check on second loop=15 >= 10 -> timed out
        mock_monotonic.side_effect = [0.0, 5.0, 15.0]

        dest_client.get.side_effect = [
            # Loop 1: find_prearchive_entry -> RECEIVING
            _make_response(
                json_data={
                    "ResultSet": {
                        "Result": [{"name": "EXP001", "status": "RECEIVING", "timestamp": "ts"}]
                    }
                }
            ),
            # Loop 2: find_prearchive_entry -> still RECEIVING
            _make_response(
                json_data={
                    "ResultSet": {
                        "Result": [{"name": "EXP001", "status": "RECEIVING", "timestamp": "ts"}]
                    }
                }
            ),
            # Timeout branch: count_dest_scans -> 1
            _make_response(json_data={"ResultSet": {"Result": [{"ID": "1"}]}}),
        ]

        actual = executor.wait_for_archive("DST", "SUB001", "EXP001", 5, timeout=10, interval=0.01)
        assert actual == 1

    @patch("xnatctl.services.transfer.executor.time.sleep")
    def test_resolves_conflict_with_overwrite(
        self,
        mock_sleep: MagicMock,
        executor: TransferExecutor,
        dest_client: MagicMock,
    ) -> None:
        """CONFLICT prearchive entry -> archive with overwrite=append."""
        dest_client.get.side_effect = [
            # Poll 1: find_prearchive_entry -> CONFLICT
            _make_response(
                json_data={
                    "ResultSet": {
                        "Result": [
                            {
                                "name": "EXP001",
                                "status": "CONFLICT",
                                "timestamp": "20260101_100000",
                            }
                        ]
                    }
                }
            ),
            # Poll 2: find_prearchive_entry -> empty (archived)
            _make_response(json_data={"ResultSet": {"Result": []}}),
            # Poll 2: count_dest_scans -> 5
            _make_response(json_data={"ResultSet": {"Result": [{"ID": str(i)} for i in range(5)]}}),
        ]
        dest_client.post.return_value = _make_response(text="OK")

        actual = executor.wait_for_archive("DST", "SUB001", "EXP001", 5, timeout=60, interval=0.01)
        assert actual == 5
        # Verify archive_prearchive was called with overwrite=append
        call_args = dest_client.post.call_args
        assert call_args[1]["params"]["overwrite"] == "append"


# -- Sample XNAT experiment XML for XML overlay tests --

_SAMPLE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<xnat:MRSession xmlns:xnat="http://nrg.wustl.edu/xnat"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://nrg.wustl.edu/xnat https://src.example.org/schemas/xnat/xnat.xsd"
    ID="XNAT_E001" label="EXP001" project="SRC"
    session_type="Guimond, Synthia^Development"
    modality="MR" UID="1.2.3.4.5">
  <!-- hidden_fields[internal_db_ref] -->
  <xnat:subject_ID>XNAT_S001</xnat:subject_ID>
  <xnat:prearchivePath>/data/prearchive/SRC/20260101/EXP001</xnat:prearchivePath>
  <xnat:date>2026-01-01</xnat:date>
  <xnat:time>10:00:00</xnat:time>
  <xnat:acquisition_site>Site A</xnat:acquisition_site>
  <xnat:scanner manufacturer="Siemens" model="Prisma"/>
  <xnat:sharing>
    <xnat:share label="shared_exp" project="OTHER"/>
  </xnat:sharing>
  <xnat:fields>
    <xnat:field name="custom_field">value</xnat:field>
  </xnat:fields>
  <xnat:resources>
    <xnat:resource label="QC" file_count="1"/>
  </xnat:resources>
  <xnat:scans>
    <xnat:scan ID="1" type="T1w" xnat:quality="usable">
      <xnat:image_session_ID>XNAT_E001</xnat:image_session_ID>
      <xnat:series_description>T1w MPRAGE</xnat:series_description>
      <xnat:quality>usable</xnat:quality>
      <xnat:parameters>
        <xnat:tr>2300</xnat:tr>
        <xnat:te>2.98</xnat:te>
      </xnat:parameters>
      <xnat:file label="DICOM" URI="/data/experiments/XNAT_E001/scans/1/resources/DICOM"/>
    </xnat:scan>
    <xnat:scan ID="2" type="fMRI">
      <xnat:image_session_ID>XNAT_E001</xnat:image_session_ID>
      <xnat:quality>usable</xnat:quality>
    </xnat:scan>
  </xnat:scans>
  <xnat:addParam name="extra_param">extra_value</xnat:addParam>
</xnat:MRSession>
"""


class TestFetchExperimentXml:
    def test_fetches_xml_from_source(
        self, executor: TransferExecutor, source_client: MagicMock
    ) -> None:
        source_client.get.return_value = _make_response(text=_SAMPLE_XML)
        result = executor.fetch_experiment_xml("XNAT_E001")
        assert result == _SAMPLE_XML
        source_client.get.assert_called_once_with(
            "/data/experiments/XNAT_E001",
            params={"format": "xml"},
        )


class TestRewriteExperimentXml:
    def test_strips_internal_elements(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        # Flatten all local tag names
        all_tags = {
            elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag for elem in root.iter()
        }

        assert "subject_ID" not in all_tags
        assert "prearchivePath" not in all_tags
        assert "image_session_ID" not in all_tags
        assert "sharing" not in all_tags
        assert "share" not in all_tags
        assert "fields" not in all_tags
        assert "file" not in all_tags

    def test_strips_session_level_resources(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        # Session-level resources should be removed
        # But scan-level elements should remain
        all_tags = {
            elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag for elem in root.iter()
        }
        assert "resources" not in all_tags

    def test_strips_schema_location(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xsi_ns = "http://www.w3.org/2001/XMLSchema-instance"
        assert f"{{{xsi_ns}}}schemaLocation" not in root.attrib

    def test_strips_html_comments(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        assert "hidden_fields" not in cleaned

    def test_preserves_session_type(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert root.attrib.get("session_type") == "Guimond, Synthia^Development"

    def test_preserves_scan_quality(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        qualities = root.findall(f".//{{{xnat_ns}}}quality")
        assert len(qualities) == 2

    def test_preserves_scan_parameters(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        tr_elems = root.findall(f".//{{{xnat_ns}}}tr")
        assert len(tr_elems) == 1
        assert tr_elems[0].text == "2300"

    def test_preserves_acquisition_site(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        site = root.find(f"{{{xnat_ns}}}acquisition_site")
        assert site is not None
        assert site.text == "Site A"

    def test_preserves_add_param(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        params = root.findall(f"{{{xnat_ns}}}addParam")
        assert len(params) == 1
        assert params[0].text == "extra_value"

    def test_rewrites_experiment_id(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML, "XNAT_E999")
        root = ET.fromstring(cleaned)
        assert root.attrib["ID"] == "XNAT_E999"

    def test_rewrites_project_attribute(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML, dest_project="DST")
        root = ET.fromstring(cleaned)
        assert root.attrib["project"] == "DST"

    def test_preserves_id_when_no_dest(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert root.attrib["ID"] == "XNAT_E001"

    def test_preserves_project_when_no_dest(self, executor: TransferExecutor) -> None:
        cleaned = executor._rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert root.attrib["project"] == "SRC"


class TestApplyXmlOverlay:
    def test_fetches_rewrites_and_puts(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        source_client.get.return_value = _make_response(text=_SAMPLE_XML)
        dest_client.get.return_value = _make_response(
            json_data={"ResultSet": {"Result": [{"ID": "XNAT_E999"}]}}
        )
        dest_client.put.return_value = _make_response(text="OK")

        executor.apply_xml_overlay(
            source_experiment_id="XNAT_E001",
            dest_project="DST",
            dest_subject="SUB001",
            dest_experiment_label="EXP001",
        )

        # Verify source XML was fetched
        source_client.get.assert_called_once_with(
            "/data/experiments/XNAT_E001",
            params={"format": "xml"},
        )

        # Verify PUT was called with text/xml content type
        dest_client.put.assert_called_once()
        call_args = dest_client.put.call_args
        assert "/data/projects/DST/subjects/SUB001/experiments/EXP001" in call_args[0][0]
        assert call_args[1]["headers"]["Content-Type"] == "text/xml"

        # Verify the XML was cleaned, ID and project rewritten
        put_data = call_args[1]["data"]
        root = ET.fromstring(put_data)
        assert root.attrib["ID"] == "XNAT_E999"
        assert root.attrib["project"] == "DST"

    def test_handles_missing_dest_experiment(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """When dest experiment not found, ID is preserved from source."""
        source_client.get.return_value = _make_response(text=_SAMPLE_XML)
        dest_client.get.return_value = _make_response(json_data={"ResultSet": {"Result": []}})
        dest_client.put.return_value = _make_response(text="OK")

        executor.apply_xml_overlay(
            source_experiment_id="XNAT_E001",
            dest_project="DST",
            dest_subject="SUB001",
            dest_experiment_label="EXP001",
        )

        put_data = dest_client.put.call_args[1]["data"]
        root = ET.fromstring(put_data)
        # ID not rewritten when check_experiment_exists returns None
        assert root.attrib["ID"] == "XNAT_E001"
