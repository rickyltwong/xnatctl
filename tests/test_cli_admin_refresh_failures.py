from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ServerError


def _config() -> Config:
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="admin",
                password="adminpass",
                verify_ssl=False,
            )
        },
    )


@pytest.mark.usefixtures("clean_root")
def test_one_failed_refresh_is_reported_and_the_rest_still_run(tmp_path: Path) -> None:
    log_file = tmp_path / "diag.jsonl"
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.get_json.return_value = {
        "ResultSet": {
            "Result": [
                {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "a"},
                {"ID": "XNAT_E002", "subject_ID": "XNAT_S002", "label": "b"},
            ]
        }
    }
    client.post.side_effect = [
        ServerError(500, "POST", "/data/services/refresh/catalog", "boom"),
        MagicMock(status_code=200),
    ]

    # pytest's capture handlers on the root logger would stop setup_logging from
    # adding the console handler, and the Traceback check below needs that handler.
    logging.getLogger().handlers = []
    with core_config_seam(_config()), config_seam(_config()):
        with patch("xnatctl.cli.common.XNATClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "--log-file",
                    str(log_file),
                    "admin",
                    "refresh-catalogs",
                    "TESTPROJ",
                    "--workers",
                    "1",
                ],
            )

    assert result.exit_code == 0, result.output
    assert "Refreshed 1 experiments" in result.output
    assert "Failed to refresh 1 experiments" in result.output
    assert "  - XNAT_E001: HTTP 500 on POST /data/services/refresh/catalog" in result.output
    assert "Traceback" not in result.output
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    failures = [r for r in records if r["logger"] == "xnatctl.cli.admin"]
    assert [(r["level"], r["msg"]) for r in failures] == [
        ("ERROR", "refresh-catalogs failed for XNAT_E001")
    ]
    assert "ServerError: HTTP 500" in failures[0]["exc"]
