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
    return TransferConfig(source_project="SRC", dest_project="DST")


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
        assert r.success is True


class TestDryRun:
    def test_dry_run_does_not_transfer(
        self,
        orchestrator: TransferOrchestrator,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        source_client.get.return_value = _make_response({
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
        })

        result = orchestrator.run(dry_run=True)

        assert result.subjects_synced == 0
        dest_client.put.assert_not_called()
        dest_client.post.assert_not_called()


class TestCircuitBreaker:
    def test_aborts_after_max_failures(
        self, orchestrator: TransferOrchestrator
    ) -> None:
        orchestrator.config.max_failures = 2
        assert orchestrator._should_abort(consecutive_failures=2) is True
        assert orchestrator._should_abort(consecutive_failures=1) is False
