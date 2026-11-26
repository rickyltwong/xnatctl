from __future__ import annotations

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.services.anon import AnonymizeService

PROJECT_EXISTS_RESPONSE = {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}


class TestGetSiteScript:
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
    def test_get_default_script(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            text="// Default XNAT anonymization script", content_type="text/plain"
        )
        service = AnonymizeService(fake_client)

        result = service.get_default_script()

        assert "Default XNAT" in result
        fake_client.get.assert_called_once_with("/xapi/anonymize/default")


class TestSiteEnabled:
    def test_site_enabled_reads_bare_boolean(self, fake_client) -> None:
        fake_client.get_json.return_value = True
        service = AnonymizeService(fake_client)

        assert service.site_enabled() is True
        fake_client.get_json.assert_called_once_with("/xapi/anonymize/site/enabled")

    def test_set_site_enabled_sends_query_param_and_json_body(
        self, fake_client, response_factory
    ) -> None:
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
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory(text="", content_type="text/plain", status_code=204),
        ]
        service = AnonymizeService(fake_client)

        assert service.get_project_script("PROJ01") is None

    def test_get_project_script_unknown_project_raises(self, fake_client) -> None:
        fake_client.get.side_effect = ResourceNotFoundError("project", "NOSUCH")
        service = AnonymizeService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_project_script("NOSUCH")


class TestSetProjectScript:
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
    def test_project_enabled_false_when_no_script_ever_set(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get.return_value = response_factory(PROJECT_EXISTS_RESPONSE)
        fake_client.get_json.return_value = False
        service = AnonymizeService(fake_client)

        assert service.project_enabled("PROJ01") is False
        fake_client.get_json.assert_called_once_with("/xapi/anonymize/projects/PROJ01/enabled")


class TestProjectHasScript:
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
    def test_passes_when_script_configured(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory({"ResultSet": {"Result": [{"status": "enabled"}]}}),
        ]
        service = AnonymizeService(fake_client)

        service.check_project_enable_scope("PROJ01")

    def test_raises_when_no_script_ever_set(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            ResourceNotFoundError("resource", "path"),
        ]
        service = AnonymizeService(fake_client)

        with pytest.raises(XNATCtlError, match="no anonymization script set"):
            service.check_project_enable_scope("PROJ01")

    def test_passes_when_script_disabled_but_present(self, fake_client, response_factory) -> None:
        fake_client.get.side_effect = [
            response_factory(PROJECT_EXISTS_RESPONSE),
            response_factory({"ResultSet": {"Result": [{"status": "disabled"}]}}),
        ]
        service = AnonymizeService(fake_client)

        service.check_project_enable_scope("PROJ01")


class TestSetProjectEnabled:
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
