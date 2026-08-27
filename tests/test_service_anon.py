"""Unit tests for AnonymizeService.

Fixture shapes and status codes are the real ones observed live against
XNAT 1.9.2.1 -- 204 on an unset project script, a raw 500 (not 404) for a
project-scoped route when the project does not exist, and the ``enable``
query-parameter/JSON-body contract on the ``.../enabled`` routes.
"""

from __future__ import annotations

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.services.anon import AnonymizeService

PROJECT_EXISTS_RESPONSE = {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}


class TestGetSiteScript:
    """Tests for AnonymizeService.get_site_script/set_site_script."""

    def test_get_site_script(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            text='version "6.1"\n(0010,0010) := subject', content_type="text/plain"
        )
        service = AnonymizeService(fake_client)

        result = service.get_site_script()

        assert "6.1" in result
        fake_client.get.assert_called_once_with("/xapi/anonymize/site")

    def test_set_site_script_sends_text_plain(self, fake_client, response_factory) -> None:
        fake_client.put.return_value = response_factory(text="", content_type="text/plain")
        service = AnonymizeService(fake_client)

        service.set_site_script('version "6.1"')

        fake_client.put.assert_called_once_with(
            "/xapi/anonymize/site",
            content=b'version "6.1"',
            headers={"Content-Type": "text/plain"},
        )


class TestGetDefaultScript:
    """Tests for AnonymizeService.get_default_script -- the read-only built-in template."""

    def test_get_default_script(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            text="// Default XNAT anonymization script", content_type="text/plain"
        )
        service = AnonymizeService(fake_client)

        result = service.get_default_script()

        assert "Default XNAT" in result
        fake_client.get.assert_called_once_with("/xapi/anonymize/default")


class TestSiteEnabled:
    """Tests for AnonymizeService.site_enabled/set_site_enabled."""

    def test_site_enabled_reads_bare_boolean(self, fake_client) -> None:
        fake_client.get_json.return_value = True
        service = AnonymizeService(fake_client)

        assert service.site_enabled() is True
        fake_client.get_json.assert_called_once_with("/xapi/anonymize/site/enabled")

    def test_set_site_enabled_sends_query_param_and_json_body(
        self, fake_client, response_factory
    ) -> None:
        """The ``enable`` query parameter carries the real value -- the JSON body's
        content is never read by the server, but a JSON Content-Type is required
        or the route answers 415 (verified live).
        """
        fake_client.put.return_value = response_factory(text="", content_type="application/json")
        service = AnonymizeService(fake_client)

        service.set_site_enabled(False)

        fake_client.put.assert_called_once_with(
            "/xapi/anonymize/site/enabled",
            params={"enable": "false"},
            content=b"null",
            headers={"Content-Type": "application/json"},
        )


class TestGetProjectScript:
    """Tests for AnonymizeService.get_project_script."""

    def test_get_project_script_present(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory(text='version "6.1"', content_type="text/plain", status_code=200),
        ]
        service = AnonymizeService(fake_client)

        result = service.get_project_script("PROJ01")

        assert result == 'version "6.1"'

    def test_get_project_script_unset_returns_none_on_204(
        self, fake_client, response_factory
    ) -> None:
        """204 (empty body) means "no override" -- not an error, not "" mistaken for content."""
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory(text="", content_type="text/plain", status_code=204),
        ]
        service = AnonymizeService(fake_client)

        assert service.get_project_script("PROJ01") is None

    def test_get_project_script_unknown_project_raises(self, fake_client) -> None:
        """The route itself answers a raw 500 for an unknown project (verified live);
        the project-existence preflight must turn that into a clean 404 first.
        """
        fake_client.get.side_effect = ResourceNotFoundError("project", "NOSUCH")
        service = AnonymizeService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_project_script("NOSUCH")


class TestSetProjectScript:
    """Tests for AnonymizeService.set_project_script."""

    def test_set_project_script(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(PROJECT_EXISTS_RESPONSE)
        fake_client.put.return_value = response_factory(text="", content_type="text/plain")
        service = AnonymizeService(fake_client)

        service.set_project_script("PROJ01", 'version "6.1"')

        fake_client.put.assert_called_once_with(
            "/xapi/anonymize/projects/PROJ01",
            content=b'version "6.1"',
            headers={"Content-Type": "text/plain"},
        )


class TestProjectEnabled:
    """Tests for AnonymizeService.project_enabled."""

    def test_project_enabled_false_when_no_script_ever_set(
        self, fake_client, response_factory
    ) -> None:
        """This route does not require a script to exist -- unlike the PUT form."""
        fake_client.get.return_value = response_factory(PROJECT_EXISTS_RESPONSE)
        fake_client.get_json.return_value = False
        service = AnonymizeService(fake_client)

        assert service.project_enabled("PROJ01") is False
        fake_client.get_json.assert_called_once_with("/xapi/anonymize/projects/PROJ01/enabled")


class TestProjectHasScript:
    """Tests for AnonymizeService.project_has_script -- distinguishes unset from disabled."""

    def test_true_when_config_history_exists(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            {"ResultSet": {"Result": [{"status": "disabled"}]}}
        )
        service = AnonymizeService(fake_client)

        assert service.project_has_script("PROJ01") is True
        fake_client.get.assert_called_once_with("/data/projects/PROJ01/config/anon")

    def test_false_on_404(self, fake_client) -> None:
        fake_client.get.side_effect = ResourceNotFoundError("resource", "path")
        service = AnonymizeService(fake_client)

        assert service.project_has_script("PROJ01") is False


class TestCheckProjectEnableScope:
    """Tests for AnonymizeService.check_project_enable_scope -- the enable/disable preflight."""

    def test_passes_when_script_configured(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),  # project exists
            response_factory({"ResultSet": {"Result": [{"status": "enabled"}]}}),  # has script
        ]
        service = AnonymizeService(fake_client)

        service.check_project_enable_scope("PROJ01")  # must not raise

    def test_raises_when_no_script_ever_set(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),  # project exists
            ResourceNotFoundError("resource", "path"),  # no config row at all
        ]
        service = AnonymizeService(fake_client)

        with pytest.raises(XNATCtlError, match="no anonymization script set"):
            service.check_project_enable_scope("PROJ01")

    def test_passes_when_script_disabled_but_present(self, fake_client, response_factory) -> None:
        """Regression: a DISABLED project script must NOT be mistaken for "never set" --
        GET /xapi/anonymize/projects/{p} answers 204 in both cases, so this preflight
        must consult the config-history route instead, which reports disabled entries.
        """
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory({"ResultSet": {"Result": [{"status": "disabled"}]}}),
        ]
        service = AnonymizeService(fake_client)

        service.check_project_enable_scope("PROJ01")  # must not raise


class TestSetProjectEnabled:
    """Tests for AnonymizeService.set_project_enabled."""

    def test_set_project_enabled_sends_query_param(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory({"ResultSet": {"Result": [{"status": "enabled"}]}}),
        ]
        fake_client.put.return_value = response_factory(text="", content_type="application/json")
        service = AnonymizeService(fake_client)

        service.set_project_enabled("PROJ01", True)

        fake_client.put.assert_called_once_with(
            "/xapi/anonymize/projects/PROJ01/enabled",
            params={"enable": "true"},
            content=b"null",
            headers={"Content-Type": "application/json"},
        )

    def test_set_project_enabled_refuses_without_script(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            ResourceNotFoundError("resource", "path"),
        ]
        service = AnonymizeService(fake_client)

        with pytest.raises(XNATCtlError, match="no anonymization script set"):
            service.set_project_enabled("PROJ01", True)
        fake_client.put.assert_not_called()
