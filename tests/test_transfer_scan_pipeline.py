"""Tests for ScanPipeline: subject/experiment orchestration and archive wait."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from xnatctl.core.state import TransferStateStore
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.conflicts import ConflictChecker
from xnatctl.services.transfer.discovery import ChangeType, DiscoveredEntity, DiscoveryService
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine
from xnatctl.services.transfer.orchestrator import TransferResult
from xnatctl.services.transfer.scan_pipeline import ScanPipeline
from xnatctl.services.transfer.verifier import Verifier


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
def state_store(tmp_path) -> Iterator[TransferStateStore]:
    store = TransferStateStore(tmp_path / "transfer.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def config() -> TransferConfig:
    return TransferConfig(
        source_project="SRC",
        dest_project="DST",
        scan_retry_count=1,
        scan_retry_delay=0.01,
    )


@pytest.fixture
def executor(source_client: MagicMock, dest_client: MagicMock) -> TransferExecutor:
    return TransferExecutor(source_client, dest_client)


@pytest.fixture
def filter_engine(config: TransferConfig) -> FilterEngine:
    return FilterEngine(config.filtering)


@pytest.fixture
def conflict_checker(dest_client: MagicMock) -> ConflictChecker:
    return ConflictChecker(dest_client)


@pytest.fixture
def discovery(source_client: MagicMock) -> DiscoveryService:
    return DiscoveryService(source_client)


@pytest.fixture
def verifier(source_client: MagicMock, dest_client: MagicMock) -> Verifier:
    return Verifier(source_client, dest_client)


@pytest.fixture
def pipeline(
    executor: TransferExecutor,
    filter_engine: FilterEngine,
    conflict_checker: ConflictChecker,
    discovery: DiscoveryService,
    state_store: TransferStateStore,
    verifier: Verifier,
    config: TransferConfig,
) -> ScanPipeline:
    return ScanPipeline(
        executor=executor,
        filter_engine=filter_engine,
        conflict_checker=conflict_checker,
        discovery=discovery,
        state_store=state_store,
        verifier=verifier,
        config=config,
    )


class TestUploadDicomPhase:
    def test_upload_dicom_phase_skips_existing_experiment(
        self,
        pipeline: ScanPipeline,
    ) -> None:
        """Pipelined phase skips DICOM when the destination session exists."""
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

        pipeline.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        pipeline.executor.check_experiment_exists = MagicMock(return_value="XNAT_E999")
        pipeline.executor.create_experiment = MagicMock()
        pipeline.executor.discover_scan_resources = MagicMock()
        pipeline.executor.download_scan_dicom = MagicMock()
        pipeline.executor.upload_scan_dicom = MagicMock()

        result = TransferResult()

        ctx = pipeline._upload_dicom_phase(exp, 1, "DST", subject, result)

        assert ctx is None
        pipeline.executor.create_experiment.assert_not_called()
        pipeline.executor.discover_scan_resources.assert_not_called()
        pipeline.executor.download_scan_dicom.assert_not_called()
        pipeline.executor.upload_scan_dicom.assert_not_called()
