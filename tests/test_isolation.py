"""The unit tier stays off the network and out of the developer's own files."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_socket import SocketConnectBlockedError


def test_a_remote_connect_is_refused() -> None:
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


def test_config_and_state_files_are_temporary(tmp_path_factory: pytest.TempPathFactory) -> None:
    from xnatctl.cli import config_cmd
    from xnatctl.core import auth, config, logging, update_check

    paths = {
        "CONFIG_DIR": config.CONFIG_DIR,
        "CONFIG_FILE": config.CONFIG_FILE,
        "config_cmd.CONFIG_FILE": config_cmd.CONFIG_FILE,
        "SESSION_CACHE_FILE": auth.SESSION_CACHE_FILE,
        "AUDIT_LOG_FILE": logging.AUDIT_LOG_FILE,
        "UPDATE_CACHE_FILE": update_check.UPDATE_CACHE_FILE,
    }
    base = tmp_path_factory.getbasetemp()
    outside = {name: path for name, path in paths.items() if not Path(path).is_relative_to(base)}

    assert not outside


_AMBIENT = {
    "XNAT_URL": "https://xnat.example.org",
    "XNATCTL_LOG_FILE": "/nonexistent/diagnostics.jsonl",
    "HTTPS_PROXY": "http://proxy.example.org:3128",
    "XNATCTL_TEST_URL": "http://127.0.0.1:8104",
}


@pytest.fixture(scope="module")
def ambient_environment() -> Iterator[None]:
    # Module scope sets these before the per-test cleanup runs, as a shell would.
    with pytest.MonkeyPatch.context() as patcher:
        for name, value in _AMBIENT.items():
            patcher.setenv(name, value)
        yield


@pytest.mark.usefixtures("ambient_environment")
def test_a_test_runs_without_the_shell_s_xnatctl_settings_or_proxy() -> None:
    kept = {name for name in _AMBIENT if name in os.environ}

    assert kept == {"XNATCTL_TEST_URL"}


@pytest.mark.enable_socket
@pytest.mark.usefixtures("ambient_environment")
def test_a_test_that_may_reach_the_network_keeps_its_proxy() -> None:
    kept = {name for name in _AMBIENT if name in os.environ}

    assert kept == {"HTTPS_PROXY", "XNATCTL_TEST_URL"}
