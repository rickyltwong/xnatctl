from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.services.anon import AnonymizeService

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

T = TypeVar("T")


def _eventually(read: Callable[[], T], expect: T, *, attempts: int = 5, delay: float = 0.2) -> T:
    last = read()
    for _ in range(attempts):
        if last == expect:
            return last
        time.sleep(delay)
        last = read()
    return last


_MIN_PLAUSIBLE_SCRIPT_CHARS = 20

PROBE_SCRIPT = 'version "6.1"\n(0010,0010) := "XNATCTL^PROBE"\n'
PROBE_SCRIPT_V2 = 'version "6.1"\n(0010,0010) := "XNATCTL^PROBE2"\n(0010,0020) := session\n'


@pytest.fixture
def fresh_project(xnat_client: Any) -> Iterator[str]:
    project_id = f"xctlanon{uuid.uuid4().hex[:10]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except Exception as exc:  # noqa: BLE001 -- teardown must not mask a test result
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")


@pytest.fixture
def restore_site_anon_state(xnat_client: Any) -> Iterator[None]:
    service = AnonymizeService(xnat_client)
    original_script = service.get_site_script()
    if len(original_script.strip()) < _MIN_PLAUSIBLE_SCRIPT_CHARS:
        original_script = service.get_default_script()
    original_enabled = service.site_enabled()
    try:
        yield
    finally:
        service.set_site_script(original_script)
        service.set_site_enabled(original_enabled)


class TestSiteScript:
    def test_get_site_script_returns_content_not_an_empty_body(self, xnat_client: Any) -> None:
        service = AnonymizeService(xnat_client)

        script = service.get_site_script()

        assert script.strip(), "site anonymization script GET returned an empty body"

    def test_set_and_get_site_script_round_trips(
        self, xnat_client: Any, restore_site_anon_state: None
    ) -> None:
        service = AnonymizeService(xnat_client)
        expected = PROBE_SCRIPT.rstrip("\n")

        service.set_site_script(PROBE_SCRIPT)

        result = _eventually(lambda: service.get_site_script().rstrip("\n"), expected)
        assert result == expected

    def test_site_enabled_round_trips(
        self, xnat_client: Any, restore_site_anon_state: None
    ) -> None:
        service = AnonymizeService(xnat_client)

        service.set_site_enabled(False)
        assert _eventually(service.site_enabled, False) is False

        service.set_site_enabled(True)
        assert _eventually(service.site_enabled, True) is True

    def test_get_default_script_is_the_built_in_template(self, xnat_client: Any) -> None:
        service = AnonymizeService(xnat_client)

        default_script = service.get_default_script()

        assert "Default XNAT anonymization script" in default_script


class TestProjectScript:
    def test_unset_project_script_returns_none(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        service = AnonymizeService(xnat_client)

        assert service.get_project_script(integration_project) is None
        assert service.project_has_script(integration_project) is False

    def test_set_and_get_project_script_round_trips(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        service = AnonymizeService(xnat_client)

        service.set_project_script(integration_project, PROBE_SCRIPT)

        script = service.get_project_script(integration_project)
        assert script is not None
        assert script.rstrip("\n") == PROBE_SCRIPT.rstrip("\n")
        assert service.project_has_script(integration_project) is True

    def test_project_routes_unknown_project_raise_not_found(self, xnat_client: Any) -> None:
        service = AnonymizeService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_project_script("XCTL-NO-SUCH-PROJECT")
        with pytest.raises(ResourceNotFoundError):
            service.set_project_script("XCTL-NO-SUCH-PROJECT", PROBE_SCRIPT)

    def test_enable_disable_requires_a_script_first(
        self, xnat_client: Any, fresh_project: str
    ) -> None:
        service = AnonymizeService(xnat_client)

        with pytest.raises(XNATCtlError, match="no anonymization script set"):
            service.set_project_enabled(fresh_project, True)

    def test_disabled_project_script_is_distinguishable_from_never_set(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        service = AnonymizeService(xnat_client)
        service.set_project_script(integration_project, PROBE_SCRIPT_V2)
        service.set_project_enabled(integration_project, True)
        first_read = service.get_project_script(integration_project)
        assert first_read is not None
        assert first_read.rstrip("\n") == PROBE_SCRIPT_V2.rstrip("\n")

        service.set_project_enabled(integration_project, False)

        assert service.get_project_script(integration_project) is None
        assert service.project_has_script(integration_project) is True
        service.check_project_enable_scope(integration_project)

        service.set_project_enabled(integration_project, True)
        second_read = service.get_project_script(integration_project)
        assert second_read is not None
        assert second_read.rstrip("\n") == PROBE_SCRIPT_V2.rstrip("\n")
