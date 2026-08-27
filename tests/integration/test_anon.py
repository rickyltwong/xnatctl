"""Anonymization scripts, checked against a real server.

Every endpoint AnonymizeService calls was verified once, by hand, against
this same stack: 204 on an unset project script, a raw 500 (not 404) for a
project-scoped route when the project does not exist, the ``enable``
query-parameter/JSON-body contract on the ``.../enabled`` routes, and --
the one that took the most probing to find -- that a DISABLED project
script and a NEVER-SET one both read back as 204 from the same route. This
file is what keeps that verification from going stale.

Site-scoped tests save and restore the site script/enabled state around
themselves, since this stack is shared with the rest of the integration
tier (and, in this session, other agents running concurrently against it).
"""

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
    """Retry ``read`` until it returns ``expect`` or ``attempts`` run out.

    The site-wide anon script/enabled state is a single value shared by
    every project on this XNAT instance -- and, in this session, this stack
    is shared with other agents running their own integration/probing runs
    concurrently. A write immediately followed by a read can genuinely lose
    a race to a concurrent writer elsewhere; this tolerates that instead of
    flaking on it. Returns the last value read even on exhaustion, so the
    caller's own assertion produces the real diff.
    """
    last = read()
    for _ in range(attempts):
        if last == expect:
            return last
        time.sleep(delay)
        last = read()
    return last


#: Below this, a site-script read is treated as broken rather than real --
#: the built-in default template is ~348 chars, and the probe script this
#: file writes is ~45, so anything shorter than this is neither.
_MIN_PLAUSIBLE_SCRIPT_CHARS = 20

PROBE_SCRIPT = 'version "6.1"\n(0010,0010) := "XNATCTL^PROBE"\n'
PROBE_SCRIPT_V2 = 'version "6.1"\n(0010,0010) := "XNATCTL^PROBE2"\n(0010,0020) := session\n'


@pytest.fixture
def fresh_project(xnat_client: Any) -> Iterator[str]:
    """A newly-created, never-touched project, deleted afterward.

    Distinct from ``integration_project`` (session-scoped and shared across
    this whole file, so other tests here may have already given it a
    script) -- this exists for tests that specifically need a project no
    anon script has ever been set on.
    """
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
    """Save the site script + enabled state, restore both afterward.

    The site anonymization script is the ONE global value on the whole
    instance, so a botched restore is not a local test failure -- it leaves
    every later run (and any human using this stack) with a broken script.
    Hence the guard: an empty or implausibly short read is NOT saved as the
    "original". Writing that back in the ``finally`` would persist the
    damage and make it look original to the next run, which is exactly how
    this stack ended up with a two-line probe script standing in for the
    real one. When the pre-state cannot be trusted, fall back to the
    server's own default template, which ``/xapi/anonymize/default``
    always serves.
    """
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
    """Site-wide script read/write/enable, against the real server."""

    def test_get_site_script_returns_content_not_an_empty_body(self, xnat_client: Any) -> None:
        """A site script GET returns real content, unlike an unset PROJECT
        script, which answers 204 with an empty body.

        This deliberately does NOT assert the site script still matches the
        built-in template. Any real server may have customised it, and on
        this shared stack a sibling test in this very file replaces it --
        asserting pristineness made the test pass or fail on run order
        rather than on behaviour. The invariant that actually holds is
        "site scripts have content"; ``test_get_default_script_is_the_built_in_template``
        covers the template itself, via the endpoint that really is
        immutable.
        """
        service = AnonymizeService(xnat_client)

        script = service.get_site_script()

        assert script.strip(), "site anonymization script GET returned an empty body"

    def test_set_and_get_site_script_round_trips(
        self, xnat_client: Any, restore_site_anon_state: None
    ) -> None:
        """Compared with trailing whitespace stripped (the server does not
        always preserve a script's exact trailing newline on read-back,
        verified live) and read back with retries: this is the ONE global,
        site-wide value on the whole XNAT instance, so a concurrent writer
        elsewhere (another integration run, another agent probing this same
        shared stack) can genuinely win a race against a read immediately
        following this test's own write.
        """
        service = AnonymizeService(xnat_client)
        expected = PROBE_SCRIPT.rstrip("\n")

        service.set_site_script(PROBE_SCRIPT)

        result = _eventually(lambda: service.get_site_script().rstrip("\n"), expected)
        assert result == expected

    def test_site_enabled_round_trips(
        self, xnat_client: Any, restore_site_anon_state: None
    ) -> None:
        """See test_set_and_get_site_script_round_trips: retried for the same
        shared-global-state reason.
        """
        service = AnonymizeService(xnat_client)

        service.set_site_enabled(False)
        assert _eventually(service.site_enabled, False) is False

        service.set_site_enabled(True)
        assert _eventually(service.site_enabled, True) is True

    def test_get_default_script_is_the_built_in_template(self, xnat_client: Any) -> None:
        """Read-only regardless of what the site script has been changed to."""
        service = AnonymizeService(xnat_client)

        default_script = service.get_default_script()

        assert "Default XNAT anonymization script" in default_script


class TestProjectScript:
    """Project-scoped script read/write/enable, against the real server."""

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
        """The raw route answers a 500 for a project that does not exist --
        verifies the ProjectService.get() preflight turns that into a clean
        404 instead.
        """
        service = AnonymizeService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_project_script("XCTL-NO-SUCH-PROJECT")
        with pytest.raises(ResourceNotFoundError):
            service.set_project_script("XCTL-NO-SUCH-PROJECT", PROBE_SCRIPT)

    def test_enable_disable_requires_a_script_first(
        self, xnat_client: Any, fresh_project: str
    ) -> None:
        """Regression guard: the project-scoped PUT .../enabled route 500s
        ("Couldn't find the site configuration...") until a script has been
        set at least once -- check_project_enable_scope() must turn that
        into an actionable error rather than exposing the raw 500.
        """
        service = AnonymizeService(xnat_client)

        with pytest.raises(XNATCtlError, match="no anonymization script set"):
            service.set_project_enabled(fresh_project, True)

    def test_disabled_project_script_is_distinguishable_from_never_set(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        """The bug this test guards: GET /xapi/anonymize/projects/{p} answers
        204/empty BOTH when no script was ever set AND when the current
        script is disabled -- reproduced live by disabling a freshly-set
        script and re-reading it. project_has_script() (a different route)
        is what must tell these two apart, and it is what the enable/disable
        preflight actually checks.
        """
        service = AnonymizeService(xnat_client)
        service.set_project_script(integration_project, PROBE_SCRIPT_V2)
        service.set_project_enabled(integration_project, True)
        first_read = service.get_project_script(integration_project)
        assert first_read is not None
        assert first_read.rstrip("\n") == PROBE_SCRIPT_V2.rstrip("\n")

        service.set_project_enabled(integration_project, False)

        # The plain read now looks exactly like "never configured"...
        assert service.get_project_script(integration_project) is None
        # ...but project_has_script() knows better, and re-enabling must work
        # without requiring anon set to be run again.
        assert service.project_has_script(integration_project) is True
        service.check_project_enable_scope(integration_project)  # must not raise

        service.set_project_enabled(integration_project, True)
        second_read = service.get_project_script(integration_project)
        assert second_read is not None
        assert second_read.rstrip("\n") == PROBE_SCRIPT_V2.rstrip("\n")
