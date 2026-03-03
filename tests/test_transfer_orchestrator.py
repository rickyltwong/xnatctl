"""Tests for transfer orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.state import TransferStateStore
from xnatctl.models.transfer import TransferConfig
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
