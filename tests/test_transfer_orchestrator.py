"""Tests for transfer orchestrator."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from xnatctl.core.state import TransferStateStore
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.discovery import ChangeType, DiscoveredEntity
from xnatctl.services.transfer.orchestrator import (
    TransferOrchestrator,
    TransferResult,
)


def _make_response(json_data: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": "application/json"}
    resp.status_code = 200
    return resp


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
def state_store(tmp_path) -> TransferStateStore:
    return TransferStateStore(tmp_path / "transfer.db")


@pytest.fixture
def config() -> TransferConfig:
    return TransferConfig(
        source_project="SRC",
        dest_project="DST",
        scan_retry_count=1,
        scan_retry_delay=0.01,
    )


@pytest.fixture
def orchestrator(
    source_client: MagicMock,
    dest_client: MagicMock,
    state_store: TransferStateStore,
    config: TransferConfig,
) -> TransferOrchestrator:
    return TransferOrchestrator(
        source_client=source_client,
        dest_client=dest_client,
        state_store=state_store,
        config=config,
    )


class TestTransferResult:
    def test_default_values(self) -> None:
        r = TransferResult()
        assert r.subjects_synced == 0
        assert r.subjects_failed == 0
        assert r.scans_synced == 0
        assert r.scans_failed == 0
        assert r.resources_synced == 0
        assert r.resources_failed == 0
        assert r.success is True


class TestDryRun:
    def test_dry_run_does_not_transfer(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        source_client.get.return_value = _make_response(
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_S001",
                            "label": "SUB001",
                            "project": "SRC",
                            "insert_date": "2026-01-01 10:00:00.0",
                            "last_modified": "2026-01-01 10:00:00.0",
                        },
                    ]
                }
            }
        )

        result = orchestrator.run(dry_run=True)

        assert result.subjects_synced == 0
        assert result.subjects_skipped == 1
        dest_client.put.assert_not_called()
        dest_client.post.assert_not_called()

    def test_dry_run_does_not_mutate_state(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        state_store: TransferStateStore,
    ) -> None:
        source_client.get.return_value = _make_response({"ResultSet": {"Result": []}})

        orchestrator.run(dry_run=True)

        history = state_store.get_sync_history("https://src.example.org", "SRC")
        assert len(history) == 0


class TestCircuitBreaker:
    def test_aborts_after_max_failures(self, orchestrator: TransferOrchestrator) -> None:
        orchestrator.config.max_failures = 2
        assert orchestrator._should_abort(consecutive_failures=2) is True
        assert orchestrator._should_abort(consecutive_failures=1) is False


class TestRemoteIdMapping:
    def test_stores_actual_remote_id(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
        state_store: TransferStateStore,
    ) -> None:
        """Verify that the actual XNAT-assigned ID is stored, not local_id."""
        # Discovery returns one subject with no experiments
        source_client.get.side_effect = [
            # discover_subjects
            _make_response(
                {
                    "ResultSet": {
                        "Result": [
                            {
                                "ID": "XNAT_S001",
                                "label": "SUB001",
                                "project": "SRC",
                                "insert_date": "2026-01-01 10:00:00.0",
                                "last_modified": "2026-01-01 10:00:00.0",
                            }
                        ]
                    }
                }
            ),
            # discover_experiments (empty)
            _make_response({"ResultSet": {"Result": []}}),
        ]

        # create_subject returns a URI with the dest-assigned ID
        dest_client.put.return_value = MagicMock(
            text="/data/subjects/XNAT_S999", strip=lambda: None
        )
        dest_client.put.return_value.text = "/data/subjects/XNAT_S999"

        orchestrator.run()

        remote_id = state_store.get_remote_id(
            "https://src.example.org",
            "SRC",
            "https://dst.example.org",
            "DST",
            "XNAT_S001",
        )
        assert remote_id == "XNAT_S999"


class TestSuccessPropagation:
    def test_experiment_failure_sets_success_false(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Verify result.success becomes False when an experiment fails."""
        source_client.get.side_effect = [
            # discover_subjects
            _make_response(
                {
                    "ResultSet": {
                        "Result": [
                            {
                                "ID": "XNAT_S001",
                                "label": "SUB001",
                                "project": "SRC",
                                "insert_date": "2026-01-01 10:00:00.0",
                            }
                        ]
                    }
                }
            ),
            # discover_experiments
            _make_response(
                {
                    "ResultSet": {
                        "Result": [
                            {
                                "ID": "XNAT_E001",
                                "label": "EXP001",
                                "project": "SRC",
                                "xsiType": "xnat:mrSessionData",
                                "insert_date": "2026-01-01 10:00:00.0",
                            }
                        ]
                    }
                }
            ),
            # discover_scans (empty -> no DICOM -> experiment pre-created)
            _make_response({"ResultSet": {"Result": []}}),
        ]

        # check_experiment_exists -> not found, then create fails
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": []}})
        # create_subject succeeds, create_experiment raises
        subject_resp = MagicMock()
        subject_resp.text = "/data/subjects/XNAT_S999"
        dest_client.put.side_effect = [
            subject_resp,
            RuntimeError("experiment creation failed"),
        ]

        result = orchestrator.run()

        assert result.success is False
        assert result.experiments_failed >= 1


class TestPrearchiveResolution:
    def test_wait_for_prearchive_calls_executor(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """_wait_for_prearchive_resolution delegates to executor.wait_for_archive."""
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

        orchestrator.executor.wait_for_archive = MagicMock(return_value=5)

        orchestrator._wait_for_prearchive_resolution(exp, "DST", subject, 5)

        orchestrator.executor.wait_for_archive.assert_called_once_with(
            dest_project="DST",
            subject_label="SUB001",
            experiment_label="EXP001",
            expected_scans=5,
            timeout=orchestrator.config.archive_wait_timeout,
            interval=orchestrator.config.archive_poll_interval,
        )

    def test_partial_archive_logs_warning(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """Warns when fewer scans than expected are archived."""
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

        orchestrator.executor.wait_for_archive = MagicMock(return_value=2)

        with patch("xnatctl.services.transfer.orchestrator.logger") as mock_logger:
            orchestrator._wait_for_prearchive_resolution(exp, "DST", subject, 5)
            mock_logger.warning.assert_called_once()


class TestTwoPhaseTransferScans:
    def test_dicom_only_skips_non_dicom(
        self,
        orchestrator: TransferOrchestrator,
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

        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "SNAPSHOTS", "file_count": "2"},
            ]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.transfer_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            orchestrator._transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=True
            )

        orchestrator.executor.transfer_scan_dicom.assert_called_once()
        orchestrator.executor.transfer_resource.assert_not_called()

    def test_non_dicom_only_skips_dicom(
        self,
        orchestrator: TransferOrchestrator,
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

        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "NII", "file_count": "1"},
            ]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.transfer_resource = MagicMock()

        result = TransferResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            orchestrator._transfer_scans(
                scans, exp, "DST", subject, Path(tmpdir), result, dicom_only=False
            )

        orchestrator.executor.transfer_scan_dicom.assert_not_called()
        orchestrator.executor.transfer_resource.assert_called_once()

    def test_shared_cache_prevents_double_fetch(
        self,
        orchestrator: TransferOrchestrator,
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

        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "NII", "file_count": "1"},
            ]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.transfer_resource = MagicMock()

        result = TransferResult()
        cache: dict[str, list[dict[str, str]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            orchestrator._transfer_scans(
                scans,
                exp,
                "DST",
                subject,
                Path(tmpdir),
                result,
                dicom_only=True,
                scan_resources_cache=cache,
            )
            orchestrator._transfer_scans(
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
        orchestrator.executor.discover_scan_resources.assert_called_once()


class TestXmlMetadataOverlay:
    def test_xml_overlay_called_after_prearchive(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """XML overlay is called between prearchive wait and non-DICOM resources."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[])
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result)

        orchestrator.executor.apply_xml_overlay.assert_called_once_with(
            source_experiment_id="XNAT_E001",
            dest_project="DST",
            dest_subject="SUB001",
            dest_experiment_label="EXP001",
        )

    def test_xml_overlay_disabled(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """XML overlay is skipped when transfer_xml_metadata=False."""
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

        orchestrator.config.transfer_xml_metadata = False
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[])
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result)

        orchestrator.executor.apply_xml_overlay.assert_not_called()

    def test_xml_overlay_failure_non_fatal(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """XML overlay failure does not block the transfer."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[])
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock(
            side_effect=RuntimeError("XML overlay failed")
        )

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        with patch("xnatctl.services.transfer.orchestrator.logger") as mock_logger:
            orchestrator._transfer_experiment(exp, 1, "DST", subject, result)
            mock_logger.warning.assert_called_once()

        # Transfer still succeeds
        assert result.experiments_synced == 1

    def test_xml_overlay_progress_callback(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """Progress callback is invoked after successful XML overlay."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[])
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False
        callback = MagicMock()

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result, callback)

        # Verify callback was called with XML overlay message
        xml_calls = [c for c in callback.call_args_list if "XML metadata overlay" in str(c)]
        assert len(xml_calls) == 1


class TestDeferredExperimentCreation:
    """Experiment should NOT be pre-created when DICOM scans exist.

    DICOM auto-archive creates the experiment; pre-creating causes
    a prearchive CONFLICT.
    """

    def test_no_precreate_when_transferable_dicom_exists(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """Experiment is not pre-created when scans have transferable DICOM."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result)

        orchestrator.executor.create_experiment.assert_not_called()

    def test_precreate_when_dicom_excluded_by_filter(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """Experiment IS pre-created when DICOM exists but filter excludes it."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[
                {"label": "DICOM", "file_count": "100"},
                {"label": "SNAPSHOTS", "file_count": "2"},
            ]
        )
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.transfer_resource = MagicMock()
        orchestrator.executor.create_scan = MagicMock()

        # Mock filter to exclude DICOM resources
        orchestrator.filter_engine.should_include_scan_resource = MagicMock(
            side_effect=lambda xsi, label: label != "DICOM"
        )

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result)

        orchestrator.executor.create_experiment.assert_called_once()

    def test_precreate_when_no_dicom_scans(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """Experiment IS pre-created when no scans have DICOM resources."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "SNAPSHOTS", "file_count": "2"}]
        )
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.transfer_resource = MagicMock()
        orchestrator.executor.create_scan = MagicMock()

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result)

        orchestrator.executor.create_experiment.assert_called_once()

    def test_skip_precreate_when_experiment_already_exists(
        self,
        orchestrator: TransferOrchestrator,
    ) -> None:
        """Experiment is not created when it already exists on destination."""
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

        orchestrator.executor.check_experiment_exists = MagicMock(return_value="XNAT_E999")
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.discover_scans = MagicMock(return_value=[])
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.apply_xml_overlay = MagicMock()

        result = TransferResult()
        orchestrator.config.verify_after_transfer = False

        orchestrator._transfer_experiment(exp, 1, "DST", subject, result)

        orchestrator.executor.create_experiment.assert_not_called()
