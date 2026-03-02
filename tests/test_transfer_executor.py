"""Tests for transfer executor."""

from __future__ import annotations

import tempfile
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
        call_args = dest_client.put.call_args
        assert "DST" in call_args[0][0]
        assert "SUB001" in call_args[0][0]
        assert result is not None


class TestCreateEmptyExperiment:
    def test_create_empty_experiment_puts_to_dest(
        self, executor: TransferExecutor, dest_client: MagicMock
    ) -> None:
        dest_client.put.return_value = _make_response(text="/data/experiments/XNAT_E001")

        result = executor.create_empty_experiment("DST", "SUB001", "EXP001", "xnat:mrSessionData")

        dest_client.put.assert_called_once()
        call_args = dest_client.put.call_args
        assert "DST" in call_args[0][0]
        assert "SUB001" in call_args[0][0]
        assert "EXP001" in call_args[0][0]
        assert call_args[1]["params"]["xsiType"] == "xnat:mrSessionData"
        assert result is not None


class TestTransferExperimentZip:
    def test_downloads_then_imports(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        # Mock the streaming download
        stream_ctx = MagicMock()
        stream_resp = MagicMock()
        stream_resp.headers = {"content-length": "100"}
        stream_resp.iter_bytes.return_value = [b"PK" + b"\x00" * 98]
        stream_resp.raise_for_status = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=stream_resp)
        stream_ctx.__exit__ = MagicMock(return_value=False)

        inner_client = MagicMock()
        inner_client.stream.return_value = stream_ctx
        source_client._get_client.return_value = inner_client
        source_client._get_cookies.return_value = {}

        # Mock import response
        dest_client.post.return_value = _make_response(text="/data/experiments/E999")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = executor.transfer_experiment_zip(
                source_experiment_id="XNAT_E001",
                dest_project="DST",
                dest_subject="SUB001",
                dest_experiment_label="EXP001",
                work_dir=Path(tmpdir),
            )

        assert result is not None


class TestTransferResource:
    def test_downloads_and_uploads(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        # Mock streaming download
        stream_ctx = MagicMock()
        stream_resp = MagicMock()
        stream_resp.headers = {"content-length": "50"}
        stream_resp.iter_bytes.return_value = [b"\x00" * 50]
        stream_resp.raise_for_status = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=stream_resp)
        stream_ctx.__exit__ = MagicMock(return_value=False)

        inner_client = MagicMock()
        inner_client.stream.return_value = stream_ctx
        source_client._get_client.return_value = inner_client
        source_client._get_cookies.return_value = {}

        # Mock upload response
        dest_client.put.return_value = _make_response(text="OK")

        with tempfile.TemporaryDirectory() as tmpdir:
            total = executor.transfer_resource(
                source_path="/data/experiments/E001/resources/DICOM/files",
                dest_path="/data/experiments/E002/resources/DICOM/files",
                resource_label="DICOM",
                work_dir=Path(tmpdir),
            )

        assert total == 50
        dest_client.put.assert_called_once()

    def test_cleans_up_zip_on_failure(
        self,
        executor: TransferExecutor,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        # Mock streaming download
        stream_ctx = MagicMock()
        stream_resp = MagicMock()
        stream_resp.iter_bytes.return_value = [b"\x00" * 10]
        stream_resp.raise_for_status = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=stream_resp)
        stream_ctx.__exit__ = MagicMock(return_value=False)

        inner_client = MagicMock()
        inner_client.stream.return_value = stream_ctx
        source_client._get_client.return_value = inner_client
        source_client._get_cookies.return_value = {}

        # Upload fails
        dest_client.put.side_effect = RuntimeError("upload failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(RuntimeError, match="upload failed"):
                executor.transfer_resource(
                    source_path="/data/experiments/E001/resources/DICOM/files",
                    dest_path="/data/experiments/E002/resources/DICOM/files",
                    resource_label="DICOM",
                    work_dir=Path(tmpdir),
                )
            # ZIP should be cleaned up
            assert not (Path(tmpdir) / "DICOM.zip").exists()
