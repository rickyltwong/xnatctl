from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

DEFAULT_URL = "http://127.0.0.1:8104"
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin"

READY_TIMEOUT_S = int(os.environ.get("XNATCTL_TEST_READY_TIMEOUT", "600"))

VERSION_PATH = "/xapi/siteConfig/buildInfo"


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})

ACK_ENV = "XNATCTL_TEST_I_KNOW_THIS_IS_NOT_PRODUCTION"


def _require_ack_for_remote(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host in LOCAL_HOSTS:
        return
    if os.environ.get(ACK_ENV, "").strip().lower() in {"yes", "1", "true"}:
        return
    pytest.exit(
        f"Refusing to run the integration tier against '{host}': it creates and "
        f"deletes projects, and that host is not local. If {url} really is a "
        f"throwaway server, set {ACK_ENV}=yes.",
        returncode=2,
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The rest of the suite reaches only loopback (pytest-socket's --allow-hosts
    # in pyproject.toml). This tier reaches the server it is pointed at, through
    # any proxy on the way; server_url's host check decides which server.
    here = Path(__file__).parent
    for item in items:
        if item.path.is_relative_to(here):
            item.add_marker(pytest.mark.enable_socket)


@pytest.fixture(scope="session")
def server_url() -> str:
    url = _env("XNATCTL_TEST_URL", DEFAULT_URL).rstrip("/")
    _require_ack_for_remote(url)
    return url


@pytest.fixture(scope="session")
def credentials() -> tuple[str, str]:
    return _env("XNATCTL_TEST_USER", DEFAULT_USER), _env("XNATCTL_TEST_PASS", DEFAULT_PASS)


def _server_version(url: str) -> str:
    try:
        resp = httpx.get(f"{url}{VERSION_PATH}", timeout=10, follow_redirects=True)
        return str(resp.json().get("version", "unknown")) if resp.status_code == 200 else "unknown"
    except (httpx.HTTPError, ValueError):
        return "unknown"


def _first_run_setup(url: str, auth: tuple[str, str]) -> bool:
    resp = httpx.get(f"{url}/xapi/siteConfig", auth=auth, timeout=30)
    if resp.status_code != 200 or resp.json().get("initialized") is not False:
        return False
    httpx.post(
        f"{url}/xapi/siteConfig",
        auth=auth,
        json={
            "siteId": "xnatctl_integration",
            "siteUrl": url,
            "adminEmail": "integration@example.invalid",
            "initialized": True,
        },
        timeout=120,
    ).raise_for_status()
    return True


def _prepare_server(url: str, auth: tuple[str, str], deadline: float) -> tuple[bool, str] | None:
    initialized_here = False
    last = "no attempt made"
    while time.time() < deadline:
        try:
            initialized_here = _first_run_setup(url, auth) or initialized_here
            resp = httpx.post(f"{url}/data/JSESSION", auth=auth, timeout=15)
            if resp.status_code == 200:
                return initialized_here, "ok"
            last = f"POST /data/JSESSION -> HTTP {resp.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(5)
    return None if last else None


def _speed_up_archiving(url: str, user: str, password: str) -> None:
    try:
        httpx.post(
            f"{url}/xapi/siteConfig",
            auth=(user, password),
            json={
                "sessionXmlRebuilderInterval": 1,
                "sessionXmlRebuilderRepeat": 10000,
                "sessionArchiveTimeoutInterval": 60,
            },
            timeout=60,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        print(f"\nWARNING: could not shorten the archive timers, runs will be slow: {exc}")


@pytest.fixture(scope="session")
def xnat_server(server_url: str, credentials: tuple[str, str]) -> str:
    ready = _prepare_server(server_url, credentials, time.time() + READY_TIMEOUT_S)
    if ready is None:
        pytest.skip(
            f"could not log in to an XNAT at {server_url} within {READY_TIMEOUT_S}s. "
            "Start one with 'docker compose -f docker-compose.integration.yml "
            "up -d --wait', or set XNATCTL_TEST_URL/USER/PASS to point at your own."
        )
    initialized_here, _ = ready
    if initialized_here:
        _speed_up_archiving(server_url, *credentials)
    print(f"\nintegration tier: XNAT {_server_version(server_url)} at {server_url}")
    return server_url


@pytest.fixture(scope="session")
def xnat_client(xnat_server: str, credentials: tuple[str, str]) -> Iterator[Any]:
    from xnatctl.core.client import XNATClient

    user, password = credentials
    client = XNATClient(base_url=xnat_server, username=user, password=password, timeout=300)
    client.authenticate()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def integration_project(xnat_client: Any) -> Iterator[str]:
    from xnatctl.core.exceptions import XNATCtlError

    project_id = f"xctl{uuid.uuid4().hex[:12]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except XNATCtlError as exc:
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")
