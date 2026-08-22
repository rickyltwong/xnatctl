"""Tests for xnatctl CLI resource commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "list", "--project", "PROJ1"])

        assert result.exit_code == 0
        call_url = client.get_json.call_args[0][0]
        assert call_url == "/data/projects/PROJ1/resources"

    def test_resource_list_rejects_project_and_session(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client) as mock_xnat:
                    result = runner.invoke(cli, ["resource", "list"])

        assert result.exit_code != 0
        assert "Provide SESSION_ID or --project" in result.output
        mock_xnat.assert_not_called()
        client.get_json.assert_not_called()

    def test_resource_list_rejects_project_with_scan(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "list", "XNAT_E00001", "--quiet"])

        assert result.exit_code == 0
        assert "DICOM" in result.output

    def test_resource_list_empty(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "show", "XNAT_E00001", "DICOM"])

        assert result.exit_code == 0

    def test_resource_show_not_found(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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
        # The scan-level parent ref carries the project (for label resolution)
        # and the scan id.
        create_parent = mock_service.create.call_args.kwargs["parent"]
        upload_parent = mock_service.upload_file.call_args.kwargs["parent"]
        assert create_parent.experiment.project_id == "CLM01_UCA_4"
        assert upload_parent.experiment.project_id == "CLM01_UCA_4"
        assert upload_parent.scan_id == "1"

    def test_resource_upload_directory_passes_project(self, runner: CliRunner, tmp_path) -> None:
        """Directory upload also receives ``project=``."""
        client = _mock_client()
        mock_service = MagicMock()

        test_dir = tmp_path / "bids"
        test_dir.mkdir()
        (test_dir / "file.nii.gz").write_text("fake")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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
        upload_parent = mock_service.upload_directory.call_args.kwargs["parent"]
        assert upload_parent.experiment == "SESS"
        assert upload_parent.project_id == "MYPROJ"

    def test_resource_upload_failure(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.upload_file.side_effect = Exception("Upload failed: timeout")

        test_file = tmp_path / "test.nii.gz"
        test_file.write_text("fake data")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["resource", "refresh", "/archive/projects/MYPROJ"])

        assert result.exit_code != 0
        assert "500" in result.output
        assert "boom" in result.output

    def test_resource_refresh_rejects_unknown_option(self, runner: CliRunner) -> None:
        """``--options`` is constrained to the documented Choice set."""
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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


def _wire_stream(client: MagicMock, capture: dict) -> None:
    """Wire ``client.stream(...)`` to capture the requested URL."""
    resp = MagicMock()
    resp.headers = {"content-length": "0"}
    resp.iter_bytes.return_value = iter([b""])
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False

    def stream(method: str, url: str, **kwargs: object) -> MagicMock:
        capture["method"] = method
        capture["url"] = url
        return cm

    client.stream.side_effect = stream


class TestResourceScopeLevels:
    """Project/subject/session/scan scope for list, download, and upload."""

    # ---- list --------------------------------------------------------------

    def test_list_subject_scope_hits_subject_path(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "list", "--project", "PROJ1", "--subject", "SUB01"]
                    )

        assert result.exit_code == 0
        client.get_json.assert_called_once_with("/data/projects/PROJ1/subjects/SUB01/resources")

    def test_list_subject_without_project_rejected(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client) as mock_xnat:
                    result = runner.invoke(cli, ["resource", "list", "--subject", "SUB01"])

        assert result.exit_code != 0
        assert "--subject requires --project" in result.output
        mock_xnat.assert_not_called()

    # ---- download ----------------------------------------------------------

    @pytest.mark.parametrize(
        "args,expected_url",
        [
            (
                ["XNAT_E00001", "DICOM"],
                "/data/experiments/XNAT_E00001/resources/DICOM/files",
            ),
            (
                ["XNAT_E00001", "DICOM", "--scan", "1"],
                "/data/experiments/XNAT_E00001/scans/1/resources/DICOM/files",
            ),
            (
                ["-P", "MYPROJ", "TEMPLATEFLOW"],
                "/data/projects/MYPROJ/resources/TEMPLATEFLOW/files",
            ),
            (
                ["-P", "MYPROJ", "-S", "SUB01", "QC"],
                "/data/projects/MYPROJ/subjects/SUB01/resources/QC/files",
            ),
        ],
    )
    def test_download_scope_urls(self, runner: CliRunner, args, expected_url) -> None:
        client = _mock_client()
        capture: dict = {}
        _wire_stream(client, capture)

        with runner.isolated_filesystem():
            with core_config_seam(_mock_config()):
                with config_seam(_mock_config()):
                    with patch("xnatctl.cli.common.XNATClient", return_value=client):
                        result = runner.invoke(
                            cli, ["resource", "download", *args, "-f", "out.zip"]
                        )

        assert result.exit_code == 0, result.output
        assert capture["url"] == expected_url

    def test_download_no_scope_rejected(self, runner: CliRunner) -> None:
        client = _mock_client()

        with runner.isolated_filesystem():
            with core_config_seam(_mock_config()):
                with config_seam(_mock_config()):
                    with patch("xnatctl.cli.common.XNATClient", return_value=client):
                        # Single positional with no -P/-S is the resource label
                        # but has no parent scope.
                        result = runner.invoke(
                            cli, ["resource", "download", "TEMPLATEFLOW", "-f", "out.zip"]
                        )

        assert result.exit_code != 0
        assert "Provide a SESSION argument" in result.output

    def test_download_typed_failure_exits_cleanly(self, runner: CliRunner) -> None:
        """A typed failure from the streamer exits nonzero with a clean message.

        @handle_errors turns the propagated exception into the documented exit
        code (3 for an auth failure) and a one-line message -- no traceback, no
        stringified success summary.
        """
        from xnatctl.core.exceptions import SessionExpiredError

        client = _mock_client()

        with runner.isolated_filesystem():
            with core_config_seam(_mock_config()):
                with config_seam(_mock_config()):
                    with patch("xnatctl.cli.common.XNATClient", return_value=client):
                        with patch(
                            "xnatctl.cli.resource.stream_to_file",
                            side_effect=SessionExpiredError("https://xnat.example.org"),
                        ):
                            result = runner.invoke(
                                cli,
                                ["resource", "download", "XNAT_E00001", "DICOM", "-f", "out.zip"],
                            )

        assert result.exit_code == 3, result.output
        assert "Session expired" in result.output
        assert "Traceback" not in result.output

    # ---- upload ------------------------------------------------------------

    def test_upload_project_scope_parent(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        test_file = tmp_path / "tpl.json"
        test_file.write_text("{}")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.services.resources.ResourceService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            ["resource", "upload", "-P", "MYPROJ", "TEMPLATEFLOW", str(test_file)],
                        )

        assert result.exit_code == 0, result.output
        parent = mock_service.upload_file.call_args.kwargs["parent"]
        assert type(parent).__name__ == "ProjectRef"
        assert parent.project_id == "MYPROJ"

    def test_upload_subject_scope_parent(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        test_file = tmp_path / "qc.json"
        test_file.write_text("{}")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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
                                "-S",
                                "SUB01",
                                "QC",
                                str(test_file),
                            ],
                        )

        assert result.exit_code == 0, result.output
        parent = mock_service.upload_file.call_args.kwargs["parent"]
        assert type(parent).__name__ == "SubjectRef"
        assert parent.project_id == "MYPROJ"
        assert parent.subject == "SUB01"

    def test_upload_subject_without_project_rejected(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        test_file = tmp_path / "qc.json"
        test_file.write_text("{}")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["resource", "upload", "-S", "SUB01", "QC", str(test_file)]
                    )

        assert result.exit_code != 0
        assert "--subject requires --project" in result.output
