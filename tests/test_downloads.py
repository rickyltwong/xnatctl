"""Tests for xnatctl.services.downloads module."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xnatctl.models.progress import DownloadSummary
from xnatctl.services.downloads import DownloadService


class TestDownloadService:
    """Tests for DownloadService."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock XNAT client."""
        client = MagicMock()
        client._get_client.return_value = MagicMock()
        client._get_cookies.return_value = {"JSESSIONID": "test-token"}
        return client

    @pytest.fixture
    def download_service(self, mock_client):
        """Create a DownloadService with mock client."""
        return DownloadService(mock_client)

    def test_download_service_initialization(self, mock_client):
        """Test that DownloadService initializes with a client."""
        service = DownloadService(mock_client)
        assert service.client == mock_client


class TestDownloadResourceSessionResolution:
    """Tests for session ID resolution in download_resource."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock XNAT client."""
        client = MagicMock()
        client._get_client.return_value = MagicMock()
        client._get_cookies.return_value = {"JSESSIONID": "test-token"}
        return client

    @pytest.fixture
    def download_service(self, mock_client):
        """Create a DownloadService with mock client."""
        return DownloadService(mock_client)

    def test_resolves_session_label_to_internal_id(self, download_service, tmp_path):
        """Test that session label is resolved to internal experiment ID."""
        # Given: A session label and a mock response with internal ID
        session_label = "MY_SESSION_LABEL"
        internal_id = "XNAT_E00001"
        project = "TEST_PROJECT"

        # Mock the _get method to return experiment data
        download_service._get = MagicMock(
            return_value={"items": [{"data_fields": {"ID": internal_id}}]}
        )

        # Mock the HTTP client stream to fail (we just want to test resolution)
        mock_http_client = MagicMock()
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_http_client.stream.return_value = mock_stream_ctx
        download_service.client._get_client.return_value = mock_http_client

        # When: download_resource is called
        result = download_service.download_resource(
            session_id=session_label,
            resource_label="DICOM",
            output_dir=tmp_path,
            scan_id="1",
            project=project,
        )

        # Then: The _get was called to resolve the session
        download_service._get.assert_called_once_with(
            f"/data/projects/{project}/experiments/{session_label}",
            params={"format": "json"},
        )

        # And: The result indicates failure (due to our mock)
        assert not result.success

    def test_uses_session_id_directly_when_starts_with_xnat_e(self, download_service, tmp_path):
        """Test that XNAT_E* IDs are used directly without resolution."""
        # Given: A session ID starting with XNAT_E
        session_id = "XNAT_E12345"
        project = "TEST_PROJECT"

        download_service._get = MagicMock()

        # Mock HTTP to fail
        mock_http_client = MagicMock()
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(side_effect=Exception("Connection test"))
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_http_client.stream.return_value = mock_stream_ctx
        download_service.client._get_client.return_value = mock_http_client

        # When: download_resource is called
        download_service.download_resource(
            session_id=session_id,
            resource_label="DICOM",
            output_dir=tmp_path,
            scan_id="1",
            project=project,
        )

        # Then: _get was NOT called for resolution
        download_service._get.assert_not_called()


class TestDownloadScan:
    """Tests for download_scan method."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock XNAT client."""
        client = MagicMock()
        client._get_client.return_value = MagicMock()
        client._get_cookies.return_value = {"JSESSIONID": "test-token"}
        return client

    @pytest.fixture
    def download_service(self, mock_client):
        """Create a DownloadService with mock client."""
        return DownloadService(mock_client)

    def test_download_scan_delegates_to_download_resource(self, download_service, tmp_path):
        """Test that download_scan calls download_resource with correct params."""
        # Given: A mock for download_resource
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            session_id="TEST_SESSION",
        )
        download_service.download_resource = MagicMock(return_value=mock_summary)

        # When: download_scan is called
        result = download_service.download_scan(
            session_id="TEST_SESSION",
            scan_id="1",
            output_dir=tmp_path,
            project="TEST_PROJECT",
            resource="NIFTI",
        )

        # Then: download_resource was called with correct parameters
        download_service.download_resource.assert_called_once_with(
            session_id="TEST_SESSION",
            resource_label="NIFTI",
            output_dir=tmp_path,
            scan_id="1",
            project="TEST_PROJECT",
            progress_callback=None,
        )

        # And: The result is returned
        assert result == mock_summary


class TestDownloadResourcePathConstruction:
    """Tests for URL path construction in download_resource."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock XNAT client."""
        client = MagicMock()
        return client

    @pytest.fixture
    def download_service(self, mock_client):
        """Create a DownloadService with mock client."""
        return DownloadService(mock_client)

    def test_scan_resource_path_uses_experiments_endpoint(self, download_service, tmp_path):
        """Test that scan downloads use /data/experiments/{id}/scans/... path."""
        # Given: Mock setup
        download_service._get = MagicMock(
            return_value={"items": [{"data_fields": {"ID": "INTERNAL_ID"}}]}
        )

        mock_http_client = MagicMock()
        mock_response = MagicMock()
        mock_response.headers = {"content-length": "0"}
        mock_response.iter_bytes.return_value = []

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(return_value=mock_response)
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_http_client.stream.return_value = mock_stream_ctx
        download_service.client._get_client.return_value = mock_http_client
        download_service.client._get_cookies.return_value = {}

        # Create a valid empty ZIP file for extraction
        zip_path = tmp_path / "DICOM.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "content")

        # Patch open to return our test zip
        with patch("builtins.open", create=True):
            with patch.object(zipfile, "ZipFile") as mock_zipfile:
                mock_zf = MagicMock()
                mock_zipfile.return_value.__enter__ = MagicMock(return_value=mock_zf)
                mock_zipfile.return_value.__exit__ = MagicMock(return_value=False)

                # When: download_resource is called for scan
                download_service.download_resource(
                    session_id="SESSION_LABEL",
                    resource_label="DICOM",
                    output_dir=tmp_path,
                    scan_id="1",
                    project="PROJECT",
                )

        # Then: Stream was called with experiments-based path
        stream_call_args = mock_http_client.stream.call_args
        path_arg = stream_call_args[0][1]
        assert path_arg == "/data/experiments/INTERNAL_ID/scans/1/resources/DICOM/files"

    def test_session_resource_path_uses_experiments_endpoint(self, download_service, tmp_path):
        """Test that session-level resource downloads use experiments endpoint."""
        # Given: Mock setup (no scan_id)
        download_service._get = MagicMock(
            return_value={"items": [{"data_fields": {"ID": "INTERNAL_ID"}}]}
        )

        mock_http_client = MagicMock()
        mock_response = MagicMock()
        mock_response.headers = {"content-length": "0"}
        mock_response.iter_bytes.return_value = []

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(return_value=mock_response)
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_http_client.stream.return_value = mock_stream_ctx
        download_service.client._get_client.return_value = mock_http_client
        download_service.client._get_cookies.return_value = {}

        with patch("builtins.open", create=True):
            with patch.object(zipfile, "ZipFile") as mock_zipfile:
                mock_zf = MagicMock()
                mock_zipfile.return_value.__enter__ = MagicMock(return_value=mock_zf)
                mock_zipfile.return_value.__exit__ = MagicMock(return_value=False)

                # When: download_resource is called without scan_id
                download_service.download_resource(
                    session_id="SESSION_LABEL",
                    resource_label="SNAPSHOTS",
                    output_dir=tmp_path,
                    scan_id=None,
                    project="PROJECT",
                )

        # Then: Stream was called with session-level path
        stream_call_args = mock_http_client.stream.call_args
        path_arg = stream_call_args[0][1]
        assert path_arg == "/data/experiments/INTERNAL_ID/resources/SNAPSHOTS/files"
