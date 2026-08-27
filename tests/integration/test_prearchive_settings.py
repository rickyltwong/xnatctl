"""Project prearchive-routing settings, checked against a real server.

Every claim PrearchiveService's ``get_routing_code``/``set_routing_mode``
methods depend on was verified once, by hand, against this same stack: the
``GET`` route answers a BARE INTEGER as text, not JSON; the server accepts
and stores any integer at all (no server-side validation of the three
meaningful codes); and a fresh project defaults to code 4 (AutoArchive).
This file is what keeps that verification from going stale.

Uses its own throwaway project rather than the shared ``integration_project``
fixture -- changing prearchive routing is real, mutating project state, and
``integration_project`` is shared (session-scoped) across this whole
integration run.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.services.prearchive import (
    PREARCHIVE_CODE_TO_MODE,
    PREARCHIVE_MODE_TO_CODE,
    PrearchiveService,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


@pytest.fixture
def routing_project(xnat_client: Any) -> Iterator[str]:
    """A newly-created, never-touched project, deleted afterward."""
    project_id = f"xctlpc{uuid.uuid4().hex[:12]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except Exception as exc:  # noqa: BLE001 -- teardown must not mask a test result
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")


class TestGetRoutingCode:
    """Read the routing code of a fresh, untouched project."""

    def test_fresh_project_defaults_to_auto_archive(
        self, xnat_client: Any, routing_project: str
    ) -> None:
        service = PrearchiveService(xnat_client)

        code = service.get_routing_code(routing_project)

        assert code == 4
        assert PREARCHIVE_CODE_TO_MODE[code] == "auto-archive"


class TestSetRoutingMode:
    """Set/round-trip each of the three valid modes on a real project."""

    @pytest.mark.parametrize("mode", list(PREARCHIVE_MODE_TO_CODE))
    def test_set_routing_mode_round_trips(
        self, xnat_client: Any, routing_project: str, mode: str
    ) -> None:
        service = PrearchiveService(xnat_client)

        service.set_routing_mode(routing_project, mode)

        assert service.get_routing_code(routing_project) == PREARCHIVE_MODE_TO_CODE[mode]

    def test_manual_mode_is_always_permitted(self, xnat_client: Any, routing_project: str) -> None:
        """Setting code 0 (manual) is documented to always succeed, unlike a
        non-zero code, which XNAT can refuse site-wide via
        `project.allow-auto-archive`.
        """
        service = PrearchiveService(xnat_client)

        service.set_routing_mode(routing_project, "manual")

        assert service.get_routing_code(routing_project) == 0
