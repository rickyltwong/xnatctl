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
                    result = runner.invoke(cli, ["resource", "list", "XNAT_E00001"])

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

    def test_resource_list_with_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "label": "PROTOCOL",
                        "format": "PDF",
                        "file_count": "1",
                        "file_size": "12345",
                        "content": "DOC",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "list", "--project", "PROJ1"])

        assert result.exit_code == 0
        call_url = client.get_json.call_args[0][0]
        assert call_url == "/data/projects/PROJ1/resources"

    def test_resource_list_rejects_project_and_session(self, runner: CliRunner) -> None:
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client) as mock_xnat:
                    result = runner.invoke(
                        cli,
                        ["resource", "list", "XNAT_E00001", "--project", "PROJ1"],
                    )

        assert result.exit_code != 0
        assert "Use either SESSION_ID or --project" in result.output
        mock_xnat.assert_not_called()
        client.get_json.assert_not_called()

    def test_resource_list_requires_scope(self, runner: CliRunner) -> None:
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client) as mock_xnat:
                    result = runner.invoke(cli, ["resource", "list"])

        assert result.exit_code != 0
        assert "Provide SESSION_ID or --project" in result.output
        mock_xnat.assert_not_called()
        client.get_json.assert_not_called()

    def test_resource_list_rejects_project_with_scan(self, runner: CliRunner) -> None:
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client) as mock_xnat:
                    result = runner.invoke(
                        cli,
                        ["resource", "list", "--project", "PROJ1", "--scan", "1"],
                    )

        assert result.exit_code != 0
        assert "--scan can only be used with SESSION_ID" in result.output
        mock_xnat.assert_not_called()
        client.get_json.assert_not_called()

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
                    result = runner.invoke(cli, ["resource", "list", "XNAT_E00001", "--quiet"])

        assert result.exit_code == 0
        assert "DICOM" in result.output

    def test_resource_list_empty(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "list", "XNAT_E00001"])

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
                    result = runner.invoke(cli, ["resource", "show", "XNAT_E00001", "DICOM"])

        assert result.exit_code == 0

    def test_resource_show_not_found(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "show", "XNAT_E00001", "MISSING"])

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
        assert call_url.endswith("/data/experiments/XNAT_E00001/scans/1/resources")


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

    def test_resource_upload_passes_project(self, runner: CliRunner, tmp_path) -> None:
        """``--project/-P`` threads through ``service.create`` and ``upload_file``."""
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
                                "--project",
                                "CLM01_UCA_4",
                                "SESSION_LABEL",
                                "DICOM",
                                str(test_file),
                                "--scan",
                                "1",
                            ],
                        )

        assert result.exit_code == 0
        # ``project=`` reaches both service calls.
        assert mock_service.create.call_args[1]["project"] == "CLM01_UCA_4"
        assert mock_service.upload_file.call_args[1]["project"] == "CLM01_UCA_4"
        assert mock_service.upload_file.call_args[1]["scan_id"] == "1"

    def test_resource_upload_directory_passes_project(self, runner: CliRunner, tmp_path) -> None:
        """Directory upload also receives ``project=``."""
        client = _mock_client()
        mock_service = MagicMock()

        test_dir = tmp_path / "bids"
        test_dir.mkdir()
        (test_dir / "file.nii.gz").write_text("fake")

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
                                "-P",
                                "MYPROJ",
                                "SESS",
                                "BIDS",
                                str(test_dir),
                            ],
                        )

        assert result.exit_code == 0
        assert mock_service.upload_directory.call_args[1]["project"] == "MYPROJ"

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


class TestResourceRefresh:
    """Tests for ``resource refresh`` command (F5)."""

    def test_resource_refresh_command(self, runner: CliRunner) -> None:
        """``resource refresh URI`` POSTs to the refresh-catalog service."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        client.post.return_value = mock_resp

        uri = "/archive/projects/MYPROJ/subjects/SUBJ/experiments/EXP/scans/1/resources/DICOM"

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "resource",
                            "refresh",
                            uri,
                            "--options",
                            "append",
                            "--options",
                            "populateStats",
                        ],
                    )

        assert result.exit_code == 0
        client.post.assert_called_once()
        call_args = client.post.call_args
        assert call_args[0][0] == "/data/services/refresh/catalog"
        assert call_args[1]["params"]["resource"] == uri
        # Multiple --options values are joined with commas.
        assert call_args[1]["params"]["options"] == "append,populateStats"

    def test_resource_refresh_no_options(self, runner: CliRunner) -> None:
        """Without ``--options``, the ``options`` query param is omitted."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        uri = "/archive/projects/MYPROJ"

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "refresh", uri])

        assert result.exit_code == 0
        params = client.post.call_args[1]["params"]
        assert params == {"resource": uri}

    def test_resource_refresh_non_200_errors(self, runner: CliRunner) -> None:
        """A non-200 response surfaces as a Click error with status + body."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "boom"
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "refresh", "/archive/projects/MYPROJ"])

        assert result.exit_code != 0
        assert "500" in result.output
        assert "boom" in result.output

    def test_resource_refresh_rejects_unknown_option(self, runner: CliRunner) -> None:
        """``--options`` is constrained to the documented Choice set."""
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "resource",
                            "refresh",
                            "/archive/projects/MYPROJ",
                            "--options",
                            "bogus",
                        ],
                    )

        assert result.exit_code != 0
        client.post.assert_not_called()
