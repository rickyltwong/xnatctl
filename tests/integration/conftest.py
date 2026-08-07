"""Fixtures for the integration tier.

Every other test in this repo mocks ``XNATClient`` or patches a CLI seam.
Nothing had ever checked what XNAT actually does: whether an import lands in
the prearchive or the archive, what a scan ZIP's catalog really looks like,
whether a JSESSION survives what we think it survives. That is what this tier
is for, and it is why it talks to a real server rather than a fake.

Point it at a throwaway XNAT::

    docker compose -f docker-compose.integration.yml up -d --wait
    uv run pytest tests/integration -m integration

or at a server of your own::

    XNATCTL_TEST_URL=https://xnat.example.org \\
    XNATCTL_TEST_USER=me XNATCTL_TEST_PASS=... \\
        uv run pytest tests/integration -m integration

The whole tier skips, with the reason printed, when no server answers. It is
deselected by default (see ``addopts`` in ``pyproject.toml``), so a normal
``pytest`` run never waits on any of this.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

DEFAULT_URL = "http://127.0.0.1:8104"
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin"

#: First boot builds the entire schema. Ten minutes is generous on a laptop
#: and still bounded enough to fail rather than hang a CI job.
READY_TIMEOUT_S = int(os.environ.get("XNATCTL_TEST_READY_TIMEOUT", "600"))

#: Served as soon as the webapp is up. ``/data/version`` is not -- it 404s on
#: 1.9.x, which is worth knowing before writing a readiness check against it.
VERSION_PATH = "/xapi/siteConfig/buildInfo"


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@pytest.fixture(scope="session")
def server_url() -> str:
    """Base URL of the XNAT under test."""
    return _env("XNATCTL_TEST_URL", DEFAULT_URL).rstrip("/")


@pytest.fixture(scope="session")
def credentials() -> tuple[str, str]:
    """Username and password for the XNAT under test."""
    return _env("XNATCTL_TEST_USER", DEFAULT_USER), _env("XNATCTL_TEST_PASS", DEFAULT_PASS)


def _server_version(url: str) -> str:
    """Best-effort version string for the log line. Never fatal."""
    try:
        resp = httpx.get(f"{url}{VERSION_PATH}", timeout=10, follow_redirects=True)
        return str(resp.json().get("version", "unknown")) if resp.status_code == 200 else "unknown"
    except (httpx.HTTPError, ValueError):
        return "unknown"


def _first_run_setup(url: str, auth: tuple[str, str]) -> bool:
    """Walk a fresh XNAT past its setup page. True if this call did it.

    A brand-new instance comes up with ``initialized`` false and 302s almost
    every request to ``/setup`` -- including ``POST /data/JSESSION``, so even
    logging in fails until a human has saved the site configuration once
    through the web UI. Doing that here is what makes the tier startable from
    nothing.

    Two things about this are not guessable and cost an afternoon to find:
    ``POST /xapi/siteConfig/batch`` is itself redirected to ``/setup``, so the
    single-object ``POST /xapi/siteConfig`` is the only way in; and
    ``/xapi/siteConfig/values/initialized`` is redirected too, so the flag has
    to be read from the full ``GET /xapi/siteConfig`` document. Verified
    against XNAT 1.9.2.1.

    Already-configured servers fall straight through, which is what lets the
    same fixture point at a lab server.
    """
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
    """Poll until the server can issue a session. Returns (we_initialized_it, why).

    Readiness is defined as "``POST /data/JSESSION`` returns 200", not as "some
    endpoint answers". An earlier version waited on ``/xapi/siteConfig/buildInfo``,
    which XNAT serves while it is still finishing its first-boot database work --
    so setup ran too early, failed once, and was never retried, and the whole
    tier then died on an authentication error one second into the run. Anything
    weaker than "can log in" has that failure mode.

    Setup is attempted on every pass rather than once, because the pass where
    the server first answers is not necessarily the pass where it will accept
    a configuration write.
    """
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
    """Stop the tier from spending five minutes per upload waiting on a timer.

    An uploaded session sits in the prearchive with status RECEIVING until
    XNAT's session XML rebuilder decides it has gone quiet. Out of the box
    that is a five-minute idle window checked once a minute, so the round trip
    looks hung for most of its duration and every poll timeout has to be sized
    around a timer rather than around real work. Dropping the window to one
    minute takes the round trip from roughly six minutes to roughly one, and
    changes nothing about what is being tested.

    Only ever called on a server this fixture just initialized. Site-wide
    settings on somebody's lab XNAT are not ours to retune -- pointing the
    tier at one simply waits the default out, which the poll budgets allow.
    """
    try:
        httpx.post(
            f"{url}/xapi/siteConfig",
            auth=(user, password),
            json={
                "sessionXmlRebuilderInterval": 1,  # minutes of quiet before build
                "sessionXmlRebuilderRepeat": 10000,  # ms between sweeps
                "sessionArchiveTimeoutInterval": 60,
            },
            timeout=60,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        print(f"\nWARNING: could not shorten the archive timers, runs will be slow: {exc}")


@pytest.fixture(scope="session")
def xnat_server(server_url: str, credentials: tuple[str, str]) -> str:
    """A reachable, logged-into XNAT, or skip the tier with the reason why."""
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
    """An authenticated XNATClient, the same object the CLI builds."""
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
    """A uniquely-named project, deleted afterwards.

    Unique per run rather than a fixed name so two runs can overlap and a run
    that died without cleaning up does not poison the next one. Teardown is
    best-effort: a failure to delete must not turn a passing suite red, but it
    does need to say so.
    """
    project_id = f"xctl{uuid.uuid4().hex[:12]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except Exception as exc:  # noqa: BLE001  # teardown must not mask a test result
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")
