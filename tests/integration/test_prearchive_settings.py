from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import XNATCtlError
from xnatctl.services.prearchive import (
    PREARCHIVE_CODE_TO_MODE,
    PREARCHIVE_MODE_TO_CODE,
    PrearchiveService,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


@pytest.fixture
def routing_project(xnat_client: Any) -> Iterator[str]:
    project_id = f"xctlpc{uuid.uuid4().hex[:12]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except XNATCtlError as exc:
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")


class TestGetRoutingCode:
    def test_fresh_project_defaults_to_auto_archive(
        self, xnat_client: Any, routing_project: str
    ) -> None:
        service = PrearchiveService(xnat_client)

        code = service.get_routing_code(routing_project)

        assert code == 4
        assert PREARCHIVE_CODE_TO_MODE[code] == "auto-archive"


class TestSetRoutingMode:
    @pytest.mark.parametrize("mode", list(PREARCHIVE_MODE_TO_CODE))
    def test_set_routing_mode_round_trips(
        self, xnat_client: Any, routing_project: str, mode: str
    ) -> None:
        service = PrearchiveService(xnat_client)

        service.set_routing_mode(routing_project, mode)

        assert service.get_routing_code(routing_project) == PREARCHIVE_MODE_TO_CODE[mode]

    def test_manual_mode_is_always_permitted(self, xnat_client: Any, routing_project: str) -> None:
        service = PrearchiveService(xnat_client)

        service.set_routing_mode(routing_project, "manual")

        assert service.get_routing_code(routing_project) == 0
