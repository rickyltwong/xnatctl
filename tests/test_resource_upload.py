"""Tests for resource upload CLI behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli


class FakeClient:
    """Minimal authenticated client stub."""

    is_authenticated = True

    def whoami(self) -> dict[str, str]:
        return {"username": "tester"}


class ResourceServiceSpy:
    """Capture ResourceService usage in CLI commands."""

    last_instance: ResourceServiceSpy | None = None
    raise_on_upload: str | None = None

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.create_calls: list[dict[str, object]] = []
        self.upload_file_calls: list[dict[str, object]] = []
        self.upload_directory_calls: list[dict[str, object]] = []
        ResourceServiceSpy.last_instance = self

    @classmethod
    def reset(cls) -> None:
        cls.last_instance = None
        cls.raise_on_upload = None

    def create(
        self,
        session_id: str,
        resource_label: str,
        scan_id: str | None,
        format: str | None,
        content: str | None,
    ) -> None:
        self.create_calls.append(
            {
                "session_id": session_id,
                "resource_label": resource_label,
                "scan_id": scan_id,
                "format": format,
                "content": content,
            }
        )

    def upload_file(
        self,
        session_id: str,
        resource_label: str,
        file_path: Path,
        scan_id: str | None,
        extract: bool,
        overwrite: bool,
    ) -> None:
        if ResourceServiceSpy.raise_on_upload == "file":
            raise RuntimeError("kaboom")
        self.upload_file_calls.append(
            {
                "session_id": session_id,
                "resource_label": resource_label,
                "file_path": file_path,
                "scan_id": scan_id,
                "extract": extract,
                "overwrite": overwrite,
            }
        )

    def upload_directory(
        self,
        session_id: str,
        resource_label: str,
        directory_path: Path,
        scan_id: str | None,
        overwrite: bool,
    ) -> None:
        if ResourceServiceSpy.raise_on_upload == "directory":
            raise RuntimeError("kaboom")
        self.upload_directory_calls.append(
            {
                "session_id": session_id,
                "resource_label": resource_label,
                "directory_path": directory_path,
                "scan_id": scan_id,
                "overwrite": overwrite,
            }
        )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()

    def fake_get_client(self) -> FakeClient:
        return client

    monkeypatch.setattr("xnatctl.cli.common.Context.get_client", fake_get_client)
    return client


@pytest.fixture
def resource_service_spy(monkeypatch: pytest.MonkeyPatch) -> type[ResourceServiceSpy]:
    ResourceServiceSpy.reset()
    monkeypatch.setattr("xnatctl.services.resources.ResourceService", ResourceServiceSpy)
    return ResourceServiceSpy


def test_resource_upload_file_routes_through_service(
    runner: CliRunner,
    auth_client: FakeClient,
    resource_service_spy: type[ResourceServiceSpy],
) -> None:
    with runner.isolated_filesystem():
        file_path = Path("example.txt")
        file_path.write_text("payload")

        result = runner.invoke(
            cli,
            [
                "resource",
                "upload",
                "XNAT_E00001",
                "DICOM",
                str(file_path),
                "--scan",
                "1",
                "--content",
                "raw",
                "--format",
                "NIFTI",
            ],
        )

        assert result.exit_code == 0
        assert "Uploaded to DICOM" in result.output

        service = resource_service_spy.last_instance
        assert service is not None
        assert service.create_calls == [
            {
                "session_id": "XNAT_E00001",
                "resource_label": "DICOM",
                "scan_id": "1",
                "format": "NIFTI",
                "content": "raw",
            }
        ]
        assert service.upload_directory_calls == []
        assert len(service.upload_file_calls) == 1
        assert service.upload_file_calls[0]["file_path"] == file_path
        assert service.upload_file_calls[0]["scan_id"] == "1"
        assert service.upload_file_calls[0]["extract"] is False
        assert service.upload_file_calls[0]["overwrite"] is False


def test_resource_upload_directory_routes_through_service(
    runner: CliRunner,
    auth_client: FakeClient,
    resource_service_spy: type[ResourceServiceSpy],
) -> None:
    with runner.isolated_filesystem():
        directory_path = Path("bids")
        directory_path.mkdir()
        (directory_path / "sample.txt").write_text("payload")

        result = runner.invoke(
            cli,
            [
                "resource",
                "upload",
                "XNAT_E00002",
                "BIDS",
                str(directory_path),
            ],
        )

        assert result.exit_code == 0
        assert "Uploaded to BIDS" in result.output

        service = resource_service_spy.last_instance
        assert service is not None
        assert len(service.create_calls) == 1
        assert service.upload_file_calls == []
        assert len(service.upload_directory_calls) == 1
        assert service.upload_directory_calls[0]["directory_path"] == directory_path
        assert service.upload_directory_calls[0]["scan_id"] is None
        assert service.upload_directory_calls[0]["overwrite"] is False


def test_resource_upload_failure_returns_click_exception(
    runner: CliRunner,
    auth_client: FakeClient,
    resource_service_spy: type[ResourceServiceSpy],
) -> None:
    resource_service_spy.raise_on_upload = "file"

    with runner.isolated_filesystem():
        file_path = Path("broken.txt")
        file_path.write_text("payload")

        result = runner.invoke(
            cli,
            [
                "resource",
                "upload",
                "XNAT_E00003",
                "BROKEN",
                str(file_path),
            ],
        )

        assert result.exit_code != 0
        assert "Upload failed: kaboom" in result.output
