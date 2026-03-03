"""Tests for transfer executor."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

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
