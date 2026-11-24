from __future__ import annotations

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.services.search import SearchService

SAMPLE_ROW = {
    "allow_diff_columns": "0",
    "brief_description": "xnatctl test search",
    "description": "",
    "extension": "77",
    "id": "xnatctl_test_search",
    "layeredsequence": "",
    "root_element_name": "xnat:projectData",
    "secure": "1",
    "sort_by_element_name": "",
    "sort_by_field_id": "",
    "stored_search_info": "2",
    "tag": "",
    "users": "{admin}",
}

SAMPLE_DEFINITION_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<xdat:bundle ID="xnatctl_test_search" xmlns:xdat="http://nrg.wustl.edu/security">'
    "<xdat:root_element_name>xnat:projectData</xdat:root_element_name>"
    "</xdat:bundle>"
)


class TestListSearches:
    def test_list_searches(self, fake_client) -> None:
        fake_client.get_json.return_value = {
            "ResultSet": {"Result": [SAMPLE_ROW], "title": "Stored Searches"}
        }
        service = SearchService(fake_client)

        result = service.list_searches()

        assert result == [SAMPLE_ROW]
        fake_client.get_json.assert_called_once_with("/data/search/saved")

    def test_list_searches_empty(self, fake_client) -> None:
        fake_client.get_json.return_value = {
            "ResultSet": {"Result": [], "title": "Stored Searches"}
        }
        service = SearchService(fake_client)

        assert service.list_searches() == []

    def test_list_searches_malformed_body_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {"message": "plugin disabled"}
        service = SearchService(fake_client)

        with pytest.raises(XNATCtlError, match="ResultSet"):
            service.list_searches()


class TestGetDefinition:
    def test_get_definition_requests_xml_format(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            text=SAMPLE_DEFINITION_XML, content_type="application/xml"
        )
        service = SearchService(fake_client)

        result = service.get_definition("xnatctl_test_search")

        assert result == SAMPLE_DEFINITION_XML
        fake_client.get.assert_called_once_with(
            "/data/search/saved/xnatctl_test_search", params={"format": "xml"}
        )


class TestRun:
    def test_run_extracts_dynamic_rows(self, fake_client) -> None:
        fake_client.get_json.return_value = {
            "ResultSet": {
                "Columns": [{"key": "id", "header": "ID", "id": "ID"}],
                "Result": [{"id": "PROJ1"}],
                "totalRecords": "1",
            }
        }
        service = SearchService(fake_client)

        result = service.run("xnatctl_test_search")

        assert result == [{"id": "PROJ1"}]
        fake_client.get_json.assert_called_once_with(
            "/data/search/saved/xnatctl_test_search/results"
        )

    def test_run_empty_result_set(self, fake_client) -> None:
        fake_client.get_json.return_value = {
            "ResultSet": {"Columns": [], "Result": [], "totalRecords": "0"}
        }
        service = SearchService(fake_client)

        assert service.run("xnatctl_test_search") == []

    def test_run_malformed_body_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {"message": "plugin disabled"}
        service = SearchService(fake_client)

        with pytest.raises(XNATCtlError, match="ResultSet"):
            service.run("xnatctl_test_search")


class TestDelete:
    def test_delete(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            text=SAMPLE_DEFINITION_XML, content_type="application/xml"
        )
        fake_client.delete.return_value = response_factory(text="", content_type="text/plain")
        service = SearchService(fake_client)

        service.delete("xnatctl_test_search")

        fake_client.get.assert_called_once_with(
            "/data/search/saved/xnatctl_test_search", params={"format": "xml"}
        )
        fake_client.delete.assert_called_once_with("/data/search/saved/xnatctl_test_search")

    def test_delete_unknown_id_raises_and_does_not_delete(self, fake_client) -> None:
        fake_client.get.side_effect = ResourceNotFoundError("saved search", "bad_id")
        service = SearchService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete("bad_id")

        fake_client.delete.assert_not_called()
