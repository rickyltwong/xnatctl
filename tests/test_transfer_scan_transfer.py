"""Tests for ScanTransfer: two-phase download-then-upload of scan resources."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.discovery import ChangeType, DiscoveredEntity
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine
from xnatctl.services.transfer.orchestrator import TransferResult
from xnatctl.services.transfer.scan_transfer import ScanTransfer


@pytest.fixture
def source_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://src.example.org"
    return client


@pytest.fixture
def dest_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://dst.example.org"
    return client


@pytest.fixture
def executor(source_client: MagicMock, dest_client: MagicMock) -> TransferExecutor:
    return TransferExecutor(source_client, dest_client)


@pytest.fixture
def config() -> TransferConfig:
    return TransferConfig(
        source_project="SRC",
        dest_project="DST",
        scan_retry_count=1,
        scan_retry_delay=0.01,
    )


@pytest.fixture
def filter_engine(config: TransferConfig) -> FilterEngine:
    return FilterEngine(config.filtering)


@pytest.fixture
def scan_transfer(
    executor: TransferExecutor,
    filter_engine: FilterEngine,
    config: TransferConfig,
) -> ScanTransfer:
    return ScanTransfer(executor, filter_engine, config)


class TestTwoPhaseTransferScans:
    def test_dicom_only_skips_non_dicom(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """dicom_only=True only transfers DICOM resources."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "T1w"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "SNAPSHOTS", "file_count": "2"},
            ]
        )
        scan_transfer.executor.download_scan_dicom = MagicMock(
            return_value=Path("/tmp/scan_1_DICOM.zip")
        )
        scan_transfer.executor.upload_scan_dicom = MagicMock(return_value="/data/imported")
        scan_transfer.executor.download_resource = MagicMock()
        scan_transfer.executor.upload_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            scan_transfer.transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=True
            )

        scan_transfer.executor.download_scan_dicom.assert_called_once()
        scan_transfer.executor.upload_scan_dicom.assert_called_once()
        scan_transfer.executor.download_resource.assert_not_called()

    def test_dicom_only_includes_dicom_format_secondary_resource(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """DICOM import uses the discovered label when format is DICOM."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "DTI"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "secondary", "format": "DICOM", "file_count": "72"}]
        )
        scan_transfer.executor.download_scan_dicom = MagicMock(
            return_value=Path("/tmp/scan_1_secondary.zip")
        )
        scan_transfer.executor.upload_scan_dicom = MagicMock(return_value="/data/imported")
        scan_transfer.executor.download_resource = MagicMock()
        scan_transfer.executor.upload_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            expected_work_dir = Path(tmpdir) / "scan_1"
            scan_transfer.transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=True
            )

        scan_transfer.executor.download_scan_dicom.assert_called_once_with(
            source_experiment_id="XNAT_E001",
            scan_id="1",
            work_dir=expected_work_dir,
            resource_label="secondary",
        )
        scan_transfer.executor.upload_scan_dicom.assert_called_once()
        scan_transfer.executor.download_resource.assert_not_called()

    def test_non_dicom_only_skips_dicom(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """dicom_only=False only transfers non-DICOM resources."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "T1w"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "NII", "file_count": "1"},
            ]
        )
        scan_transfer.executor.download_scan_dicom = MagicMock()
        scan_transfer.executor.upload_scan_dicom = MagicMock()
        scan_transfer.executor.download_resource = MagicMock(
            return_value=(Path("/tmp/1_NII_flat.zip"), 100)
        )
        scan_transfer.executor.upload_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            scan_transfer.transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=False
            )

        scan_transfer.executor.download_scan_dicom.assert_not_called()
        scan_transfer.executor.download_resource.assert_called_once()
        scan_transfer.executor.upload_resource.assert_called_once()

    def test_non_dicom_existing_sync_creates_scan_shell_when_dicom_upload_skipped(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """Existing-session sync creates scans before generic resource upload."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "T1w"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "format": "DICOM", "file_count": "100"},
                {"label": "NII", "format": "NIFTI", "file_count": "1"},
            ]
        )
        scan_transfer.executor.create_scan = MagicMock()
        scan_transfer.executor.download_resource = MagicMock(
            return_value=(Path("/tmp/1_NII_flat.zip"), 100)
        )
        scan_transfer.executor.upload_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            scan_transfer.transfer_scans(
                scans,
                exp,
                "DST",
                subject,
                Path(tmpdir),
                result,
                dicom_only=False,
                create_missing_scans=True,
            )

        scan_transfer.executor.create_scan.assert_called_once_with(
            dest_project="DST",
            dest_subject="SUB001",
            dest_experiment="EXP001",
            scan_id="1",
            scan_type="T1w",
            xsi_type="xnat:mrScanData",
        )
        scan_transfer.executor.download_resource.assert_called_once()
        scan_transfer.executor.upload_resource.assert_called_once()

    def test_non_dicom_only_skips_dicom_format_secondary_resource(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """DICOM-format resources are not uploaded as generic resources."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "DTI"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "secondary", "format": "DICOM", "file_count": "72"}]
        )
        scan_transfer.executor.download_scan_dicom = MagicMock()
        scan_transfer.executor.upload_scan_dicom = MagicMock()
        scan_transfer.executor.download_resource = MagicMock()
        scan_transfer.executor.upload_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            scan_transfer.transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=False
            )

        scan_transfer.executor.download_scan_dicom.assert_not_called()
        scan_transfer.executor.download_resource.assert_not_called()
        scan_transfer.executor.upload_resource.assert_not_called()

    def test_dicom_format_secondary_uses_dicom_filter_alias(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """A DICOM-format alias obeys filters configured for DICOM."""
        scan_transfer.filter_engine.should_include_scan_resource = MagicMock(
            side_effect=lambda _xsi, label: label != "DICOM"
        )

        assert (
            scan_transfer._should_include_scan_resource(
                "xnat:mrSessionData",
                {"label": "secondary", "format": "DICOM"},
            )
            is False
        )

    def test_shared_cache_prevents_double_fetch(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """Shared scan_resources_cache reuses phase 1 results in phase 3."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "T1w"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "NII", "file_count": "1"},
            ]
        )
        scan_transfer.executor.download_scan_dicom = MagicMock(
            return_value=Path("/tmp/scan_1_DICOM.zip")
        )
        scan_transfer.executor.upload_scan_dicom = MagicMock(return_value="/data/imported")
        scan_transfer.executor.download_resource = MagicMock(
            return_value=(Path("/tmp/1_NII_flat.zip"), 100)
        )
        scan_transfer.executor.upload_resource = MagicMock()

        result = TransferResult()
        cache: dict[str, list[dict[str, str]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            scan_transfer.transfer_scans(
                scans,
                exp,
                "DST",
                subject,
                Path(tmpdir),
                result,
                dicom_only=True,
                scan_resources_cache=cache,
            )
            scan_transfer.transfer_scans(
                scans,
                exp,
                "DST",
                subject,
                Path(tmpdir),
                result,
                dicom_only=False,
                scan_resources_cache=cache,
            )

        # discover_scan_resources called once (phase 1), reused in phase 3
        scan_transfer.executor.discover_scan_resources.assert_called_once()

    def test_all_downloads_complete_before_any_upload(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """All downloads finish before any upload begins (two-phase ordering)."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "T1w"}, {"ID": "2", "type": "T2w"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )

        call_order: list[str] = []

        def track_download(**kwargs: object) -> Path:
            call_order.append(f"download_{kwargs.get('scan_id', '')}")
            return Path(f"/tmp/scan_{kwargs.get('scan_id', '')}_DICOM.zip")

        def track_upload(**kwargs: object) -> str:
            call_order.append("upload")
            return "/data/imported"

        scan_transfer.executor.download_scan_dicom = MagicMock(side_effect=track_download)
        scan_transfer.executor.upload_scan_dicom = MagicMock(side_effect=track_upload)

        result = TransferResult()
        # Force single-threaded to get deterministic ordering
        scan_transfer.config.scan_workers = 1

        with tempfile.TemporaryDirectory() as tmpdir:
            scan_transfer.transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=True
            )

        # All downloads must appear before any upload
        download_indices = [i for i, c in enumerate(call_order) if c.startswith("download")]
        upload_indices = [i for i, c in enumerate(call_order) if c == "upload"]
        assert download_indices, "Expected at least one download"
        assert upload_indices, "Expected at least one upload"
        assert max(download_indices) < min(upload_indices), (
            f"Downloads must complete before uploads. Order: {call_order}"
        )

    def test_download_failure_skips_upload_for_that_scan(
        self,
        scan_transfer: ScanTransfer,
    ) -> None:
        """Failed download is recorded; upload phase proceeds for other scans."""
        exp = DiscoveredEntity(
            local_id="XNAT_E001",
            local_label="EXP001",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        subject = DiscoveredEntity(
            local_id="XNAT_S001",
            local_label="SUB001",
            change_type=ChangeType.NEW,
        )
        scans = [{"ID": "1", "type": "T1w"}, {"ID": "2", "type": "T2w"}]

        scan_transfer.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )

        call_count = 0

        def download_side_effect(**kwargs: object) -> Path:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("download failed for scan 1")
            return Path(f"/tmp/scan_{kwargs.get('scan_id', '')}_DICOM.zip")

        scan_transfer.executor.download_scan_dicom = MagicMock(side_effect=download_side_effect)
        scan_transfer.executor.upload_scan_dicom = MagicMock(return_value="/data/imported")

        result = TransferResult()
        scan_transfer.config.scan_workers = 1

        with tempfile.TemporaryDirectory() as tmpdir:
            processed = scan_transfer.transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=True
            )

        assert processed == 1  # Only scan 2 succeeded
        assert result.scans_failed == 1
        assert result.scans_synced == 1
        assert len(result.errors) == 1
        scan_transfer.executor.upload_scan_dicom.assert_called_once()
