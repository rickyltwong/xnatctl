"""Tests for xnatctl CLI resource commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _mock_config() -> Config:
    """Build a mock Config with a default profile."""
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="testuser",
                password="testpass",
                verify_ssl=False,
            )
        },
    )


def _mock_client() -> MagicMock:
    """Build a mock XNATClient."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "testuser"}
    return client


class TestResourceList:
    """Tests for resource list command."""

    def test_resource_list(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "label": "DICOM",
                        "format": "DICOM",
                        "file_count": "100",
                        "file_size": "50000000",
                        "content": "RAW",
                    },
                    {
                        "label": "NIFTI",
                        "format": "NIFTI",
                        "file_count": "5",
                        "file_size": "20000000",
                        "content": "PROCESSED",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "list", "XNAT_E00001"]
                    )

        assert result.exit_code == 0

    def test_resource_list_with_scan(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "label": "DICOM",
                        "format": "DICOM",
                        "file_count": "50",
                        "file_size": "25000000",
                        "content": "RAW",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["resource", "list", "XNAT_E00001", "--scan", "1"],
                    )

        assert result.exit_code == 0
        call_url = client.get_json.call_args[0][0]
        assert "/scans/1/resources" in call_url

    def test_resource_list_quiet(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "label": "DICOM",
                        "format": "DICOM",
                        "file_count": "100",
                        "file_size": "50000000",
                        "content": "RAW",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "list", "XNAT_E00001", "--quiet"]
                    )

        assert result.exit_code == 0
        assert "DICOM" in result.output

    def test_resource_list_empty(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "list", "XNAT_E00001"]
                    )

        assert result.exit_code == 0


class TestResourceShow:
    """Tests for resource show command."""

    def test_resource_show(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {
                            "label": "DICOM",
                            "format": "DICOM",
                            "content": "RAW",
                            "file_count": 100,
                            "file_size": "50000000",
                        }
                    ]
                }
            },
            {
                "ResultSet": {
                    "Result": [
                        {"Name": "file1.dcm"},
                        {"Name": "file2.dcm"},
                    ]
                }
            },
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "show", "XNAT_E00001", "DICOM"]
                    )

        assert result.exit_code == 0

    def test_resource_show_not_found(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "show", "XNAT_E00001", "MISSING"]
                    )

        assert result.exit_code != 0

    def test_resource_show_with_scan(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {
                            "label": "DICOM",
                            "format": "DICOM",
                            "content": "RAW",
                            "file_count": 50,
                            "file_size": "25000000",
                        }
                    ]
                }
            },
            {"ResultSet": {"Result": [{"Name": "file1.dcm"}]}},
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "resource",
                            "show",
                            "XNAT_E00001",
                            "DICOM",
                            "--scan",
                            "1",
                        ],
                    )

        assert result.exit_code == 0
        call_url = client.get_json.call_args_list[0][0][0]
        assert "/scans/1/resources/" in call_url


class TestResourceUpload:
    """Tests for resource upload command."""

    def test_resource_upload_file(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        test_file = tmp_path / "test.nii.gz"
        test_file.write_text("fake nifti data")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.services.resources.ResourceService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "resource",
                                "upload",
                                "XNAT_E00001",
                                "NIFTI",
                                str(test_file),
                            ],
                        )

        assert result.exit_code == 0
        assert "Uploaded" in result.output

    def test_resource_upload_directory(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        test_dir = tmp_path / "dicoms"
        test_dir.mkdir()
        (test_dir / "file1.dcm").write_text("dcm1")
        (test_dir / "file2.dcm").write_text("dcm2")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.services.resources.ResourceService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "resource",
                                "upload",
                                "XNAT_E00001",
                                "DICOM",
                                str(test_dir),
                            ],
                        )

        assert result.exit_code == 0
        assert "Uploaded" in result.output
        mock_service.upload_directory.assert_called_once()

    def test_resource_upload_failure(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.upload_file.side_effect = Exception("Upload failed: timeout")

        test_file = tmp_path / "test.nii.gz"
        test_file.write_text("fake data")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.services.resources.ResourceService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "resource",
                                "upload",
                                "XNAT_E00001",
                                "NIFTI",
                                str(test_file),
                            ],
                        )

        assert result.exit_code != 0
