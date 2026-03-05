"""Tests for transfer orchestrator."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from xnatctl.core.state import SyncStatus, TransferStateStore
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


class TestPipelinedTransfer:
    """Integration tests for the pipelined transfer flow.

    The pipelined flow uploads DICOM for experiment A, enqueues to poller,
    starts uploading experiment B while poller monitors A. When A is ready,
    main thread finalizes A between uploads.
    """

    @staticmethod
    def _subject_response() -> MagicMock:
        """Build mock discover_subjects HTTP response."""
        return _make_response(
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
        )

    @staticmethod
    def _experiments_response(
        experiments: list[tuple[str, str, str]],
    ) -> MagicMock:
        """Build mock discover_experiments HTTP response.

        Args:
            experiments: List of (id, label, xsiType) tuples.
        """
        rows = [
            {
                "ID": eid,
                "label": elabel,
                "project": "SRC",
                "xsiType": xsi,
                "insert_date": "2026-01-01 10:00:00.0",
            }
            for eid, elabel, xsi in experiments
        ]
        return _make_response({"ResultSet": {"Result": rows}})

    @staticmethod
    def _setup_fast_poller(orchestrator: TransferOrchestrator) -> None:
        """Configure orchestrator for fast test polling."""
        orchestrator.config.verify_after_transfer = False
        orchestrator.config.archive_poll_interval = 0.05
        orchestrator.config.archive_wait_timeout = 5.0
        orchestrator.config.scan_workers = 1

    @staticmethod
    def _mock_create_subject(dest_client: MagicMock) -> None:
        """Mock dest_client.put to return a subject URI."""
        subject_resp = MagicMock()
        subject_resp.text = "/data/subjects/XNAT_S999"
        dest_client.put.return_value = subject_resp

    def test_pipelined_transfer_two_experiments(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Two DICOM experiments are uploaded and finalized via the pipeline."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                    ("XNAT_E002", "EXP002", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.list_prearchive_entries = MagicMock(return_value=[])
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)
        orchestrator.executor.find_prearchive_entry = MagicMock(return_value=None)
        orchestrator.executor.archive_prearchive = MagicMock()
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)
        orchestrator.executor.transfer_resource = MagicMock()

        result = orchestrator.run()

        assert result.subjects_synced == 1
        assert result.experiments_synced == 2
        assert orchestrator.executor.transfer_scan_dicom.call_count == 2
        assert orchestrator.executor.apply_xml_overlay.call_count == 2

    def test_no_dicom_experiment_finalized_immediately(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Experiment with no DICOM is pre-created and finalized without poller."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "SNAPSHOTS", "file_count": "2"}]
        )
        orchestrator.executor.create_experiment = MagicMock(return_value="OK")
        orchestrator.executor.transfer_resource = MagicMock()
        orchestrator.executor.create_scan = MagicMock()
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.list_prearchive_entries = MagicMock(return_value=[])
        orchestrator.executor.count_dest_scans = MagicMock(return_value=0)
        orchestrator.executor.wait_for_archive = MagicMock(return_value=0)

        result = orchestrator.run()

        assert result.experiments_synced == 1
        orchestrator.executor.create_experiment.assert_called_once()
        orchestrator.executor.transfer_scan_dicom.assert_not_called()

    def test_deferred_experiment_failure_continues(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Failure finalizing first experiment does not block the second."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                    ("XNAT_E002", "EXP002", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.list_prearchive_entries = MagicMock(return_value=[])
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)
        orchestrator.executor.find_prearchive_entry = MagicMock(return_value=None)
        orchestrator.executor.archive_prearchive = MagicMock()
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)
        orchestrator.executor.transfer_resource = MagicMock()

        # First experiment's finalize fails via session resource discovery error.
        # discover_session_resources is called during _finalize_experiment.
        call_count = 0

        def session_resources_side_effect(experiment_id: str) -> list:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("session resource discovery boom")
            return []

        orchestrator.executor.discover_session_resources = MagicMock(
            side_effect=session_resources_side_effect
        )
        orchestrator.executor.apply_xml_overlay = MagicMock()

        result = orchestrator.run()

        # Second experiment should still be synced
        assert result.experiments_synced >= 1
        assert orchestrator.executor.transfer_scan_dicom.call_count == 2

    def test_temp_dir_cleanup_on_success(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Temp directories are cleaned up after successful pipelined transfer."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.list_prearchive_entries = MagicMock(return_value=[])
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)
        orchestrator.executor.find_prearchive_entry = MagicMock(return_value=None)
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)
        orchestrator.executor.transfer_resource = MagicMock()

        cleanup_calls: list[str] = []
        original_tempdir = tempfile.TemporaryDirectory

        class TrackingTempDir(original_tempdir):  # type: ignore[misc]
            """TemporaryDirectory subclass that tracks cleanup calls."""

            def cleanup(self) -> None:
                cleanup_calls.append(self.name)
                super().cleanup()

        with patch(
            "xnatctl.services.transfer.orchestrator.tempfile.TemporaryDirectory",
            TrackingTempDir,
        ):
            result = orchestrator.run()

        assert result.experiments_synced == 1
        # _upload_dicom_phase creates one TemporaryDirectory per DICOM experiment
        assert len(cleanup_calls) >= 1

    def test_backward_compatible_single_experiment(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Single DICOM experiment produces the same result via pipelined path."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.list_prearchive_entries = MagicMock(return_value=[])
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)
        orchestrator.executor.find_prearchive_entry = MagicMock(return_value=None)
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)
        orchestrator.executor.transfer_resource = MagicMock()

        result = orchestrator.run()

        assert result.subjects_synced == 1
        assert result.experiments_synced == 1
        assert result.success is True
        orchestrator.executor.transfer_scan_dicom.assert_called_once()
        orchestrator.executor.apply_xml_overlay.assert_called_once()

    def test_max_pending_archives_throttle(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Pipeline throttles when max_pending_archives is reached."""
        self._setup_fast_poller(orchestrator)
        orchestrator.config.max_pending_archives = 1

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                    ("XNAT_E002", "EXP002", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.list_prearchive_entries = MagicMock(return_value=[])
        orchestrator.executor.find_prearchive_entry = MagicMock(return_value=None)
        orchestrator.executor.archive_prearchive = MagicMock()
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)
        orchestrator.executor.transfer_resource = MagicMock()

        # count_dest_scans returns 1 (archived) so poller marks ready
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)

        result = orchestrator.run()

        assert result.experiments_synced == 2
        assert orchestrator.executor.transfer_scan_dicom.call_count == 2

    def test_pipelined_prearchive_resolution(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Main thread resolves a READY prearchive entry."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                    ("XNAT_E002", "EXP002", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.transfer_resource = MagicMock()

        # Prearchive: first calls return READY entry, subsequent calls empty.
        # The poller sees the READY entry and sets needs_archive_action.
        prearchive_call_count = 0

        def prearchive_side_effect(project: str) -> list[dict]:
            nonlocal prearchive_call_count
            prearchive_call_count += 1
            if prearchive_call_count <= 2:
                return [
                    {
                        "name": "EXP001",
                        "folderName": "EXP001",
                        "status": "READY",
                        "timestamp": "20260101_100000",
                        "project": "DST",
                    }
                ]
            return []

        orchestrator.executor.list_prearchive_entries = MagicMock(
            side_effect=prearchive_side_effect
        )
        orchestrator.executor.find_prearchive_entry = MagicMock(
            return_value={
                "name": "EXP001",
                "folderName": "EXP001",
                "status": "READY",
                "timestamp": "20260101_100000",
            }
        )
        orchestrator.executor.archive_prearchive = MagicMock()
        # After archiving, count_dest_scans returns expected count
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)

        result = orchestrator.run()

        assert result.experiments_synced == 2
        # archive_prearchive should have been called for the READY entry
        orchestrator.executor.archive_prearchive.assert_called()
        # Verify overwrite is None (READY, not CONFLICT)
        first_call = orchestrator.executor.archive_prearchive.call_args_list[0]
        assert first_call.kwargs.get("overwrite") is None

    def test_pipelined_conflict_resolution(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """Main thread resolves CONFLICT prearchive entry with overwrite=append."""
        self._setup_fast_poller(orchestrator)

        source_client.get.side_effect = [
            self._subject_response(),
            self._experiments_response(
                [
                    ("XNAT_E001", "EXP001", "xnat:mrSessionData"),
                    ("XNAT_E002", "EXP002", "xnat:mrSessionData"),
                ]
            ),
        ]
        self._mock_create_subject(dest_client)

        orchestrator.executor.discover_scans = MagicMock(return_value=[{"ID": "1", "type": "T1w"}])
        orchestrator.executor.check_experiment_exists = MagicMock(return_value=None)
        orchestrator.executor.discover_scan_resources = MagicMock(
            return_value=[{"label": "DICOM", "file_count": "100"}]
        )
        orchestrator.executor.transfer_scan_dicom = MagicMock()
        orchestrator.executor.apply_xml_overlay = MagicMock()
        orchestrator.executor.discover_session_resources = MagicMock(return_value=[])
        orchestrator.executor.transfer_resource = MagicMock()

        # Prearchive returns CONFLICT for first calls, then empty
        conflict_call_count = 0

        def conflict_prearchive(project: str) -> list[dict]:
            nonlocal conflict_call_count
            conflict_call_count += 1
            if conflict_call_count <= 2:
                return [
                    {
                        "name": "EXP001",
                        "folderName": "EXP001",
                        "status": "CONFLICT",
                        "timestamp": "20260101_100000",
                        "project": "DST",
                    }
                ]
            return []

        orchestrator.executor.list_prearchive_entries = MagicMock(side_effect=conflict_prearchive)
        orchestrator.executor.find_prearchive_entry = MagicMock(
            return_value={
                "name": "EXP001",
                "folderName": "EXP001",
                "status": "CONFLICT",
                "timestamp": "20260101_100000",
            }
        )
        orchestrator.executor.archive_prearchive = MagicMock()
        orchestrator.executor.count_dest_scans = MagicMock(return_value=1)
        orchestrator.executor.wait_for_archive = MagicMock(return_value=1)

        result = orchestrator.run()

        assert result.experiments_synced == 2
        orchestrator.executor.archive_prearchive.assert_called()
        # Check overwrite="append" was used for CONFLICT resolution
        archive_calls = orchestrator.executor.archive_prearchive.call_args_list
        conflict_resolved = any(call.kwargs.get("overwrite") == "append" for call in archive_calls)
        assert conflict_resolved, (
            "Expected archive_prearchive called with overwrite='append' for CONFLICT"
        )


class TestDestReconciliation:
    """Tests for reconciliation of previously-synced subjects deleted from dest."""

    @staticmethod
    def _subject_response(subjects: list[tuple[str, str]]) -> MagicMock:
        """Build mock discover_subjects HTTP response.

        Args:
            subjects: List of (id, label) tuples.
        """
        rows = [
            {
                "ID": sid,
                "label": slabel,
                "project": "SRC",
                "insert_date": "2026-01-01 10:00:00.0",
                "last_modified": "2026-01-01 10:00:00.0",
            }
            for sid, slabel in subjects
        ]
        return _make_response({"ResultSet": {"Result": rows}})

    @staticmethod
    def _experiments_response() -> MagicMock:
        """Empty experiments response."""
        return _make_response({"ResultSet": {"Result": []}})

    def test_reconcile_re_syncs_deleted_subject(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
        state_store: TransferStateStore,
    ) -> None:
        """Subject deleted from dest is re-synced on next run."""
        # Simulate a prior successful sync
        src_url = str(source_client.base_url)
        dst_url = str(dest_client.base_url)
        prev_sync = state_store.start_sync(src_url, "SRC", dst_url, "DST")
        state_store.end_sync(prev_sync, SyncStatus.COMPLETED, subjects_synced=1)
        state_store.save_id_mapping(
            src_url, "SRC", dst_url, "DST", "XNAT_S001", "XNAT_S999", "subject"
        )

        # Source: subject unchanged (insert_date == last_modified < last_sync)
        # Incremental discovery returns empty (nothing changed)
        # Full discovery returns the subject
        source_client.get.side_effect = [
            # Incremental discover_subjects: returns subject but _classify_change filters it
            self._subject_response([("XNAT_S001", "SUB001")]),
            # list_dest_subjects (from reconciliation) is on dest_client
            # Full discover_subjects (from reconciliation)
            self._subject_response([("XNAT_S001", "SUB001")]),
            # discover_experiments for the re-synced subject
            self._experiments_response(),
        ]

        # Dest: subject does NOT exist (deleted)
        dest_client.get.side_effect = [
            # list_dest_subjects: empty (subject was deleted)
            _make_response({"ResultSet": {"Result": []}}),
            # conflict_checker.check_subject: empty (subject gone)
            _make_response({"ResultSet": {"Result": []}}),
        ]

        # create_subject returns URI
        subject_resp = MagicMock()
        subject_resp.text = "/data/subjects/XNAT_S999"
        dest_client.put.return_value = subject_resp

        result = orchestrator.run()

        assert result.subjects_synced == 1
        # create_subject was called (PUT to dest)
        dest_client.put.assert_called()

    def test_no_reconcile_when_dest_has_subject(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
        state_store: TransferStateStore,
    ) -> None:
        """Subject still on dest is not re-synced."""
        src_url = str(source_client.base_url)
        dst_url = str(dest_client.base_url)
        prev_sync = state_store.start_sync(src_url, "SRC", dst_url, "DST")
        state_store.end_sync(prev_sync, SyncStatus.COMPLETED, subjects_synced=1)
        state_store.save_id_mapping(
            src_url, "SRC", dst_url, "DST", "XNAT_S001", "XNAT_S999", "subject"
        )

        # Source: subject unchanged
        source_client.get.side_effect = [
            self._subject_response([("XNAT_S001", "SUB001")]),
        ]

        # Dest: subject still exists
        dest_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "XNAT_S999"}]}}
        )

        result = orchestrator.run()

        # No subjects to sync (all exist on dest, unchanged on source)
        assert result.subjects_synced == 0
        dest_client.put.assert_not_called()

    def test_no_reconcile_on_first_sync(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        """First sync (no last_sync_time) skips reconciliation entirely."""
        source_client.get.side_effect = [
            # discover_subjects: new subject
            self._subject_response([("XNAT_S001", "SUB001")]),
            # discover_experiments
            self._experiments_response(),
        ]

        # No dest conflict check since no prior mapping
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": []}})

        subject_resp = MagicMock()
        subject_resp.text = "/data/subjects/XNAT_S001"
        dest_client.put.return_value = subject_resp

        result = orchestrator.run()

        assert result.subjects_synced == 1
