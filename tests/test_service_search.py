"""Unit tests for SearchService.

Fixture shapes are the real ones observed live against XNAT 1.9.2.1: a
``ResultSet`` envelope (not a bare array) from the listing, XML-only from the
definition route (its ``?format=json`` 404s even for a real search), and a
dynamic ``ResultSet.Columns``/``Result`` shape from running a saved search.
"""

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
    """Tests for SearchService.list_searches."""

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
        # A 200 with no 'ResultSet' key at all (e.g. a plugin-disabled body)
        # must not be coerced into "no saved searches" -- before the fix,
        # ResultSetEnvelope's default_factory silently validated this to an
        # empty list and list_searches() returned [] here too.
        fake_client.get_json.return_value = {"message": "plugin disabled"}
        service = SearchService(fake_client)

        with pytest.raises(XNATCtlError, match="ResultSet"):
            service.list_searches()


class TestGetDefinition:
    """Tests for SearchService.get_definition."""

    def test_get_definition_requests_xml_format(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            text=SAMPLE_DEFINITION_XML, content_type="application/xml"
        )
        service = SearchService(fake_client)

        result = service.get_definition("xnatctl_test_search")

        assert result == SAMPLE_DEFINITION_XML
        # This route has NO JSON representation (verified live: ?format=json
        # 404s for a real, existing search) -- format=xml is requested
        # explicitly, never format=json.
        fake_client.get.assert_called_once_with(
            "/data/search/saved/xnatctl_test_search", params={"format": "xml"}
        )


class TestRun:
    """Tests for SearchService.run."""

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
        # Same defect class as list_searches: a 200 with no 'ResultSet' key
        # must raise, not read as "this search returned nothing".
        fake_client.get_json.return_value = {"message": "plugin disabled"}
        service = SearchService(fake_client)

        with pytest.raises(XNATCtlError, match="ResultSet"):
            service.run("xnatctl_test_search")


class TestDelete:
    """Tests for SearchService.delete."""

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
        # DELETE itself answers 200 even for an unknown id (verified live),
        # so the only thing that can catch a typo is the get_definition()
        # preflight. Before the fix, delete() skipped straight to
        # client.delete() and reported success for a search that never
        # existed.
        fake_client.get.side_effect = ResourceNotFoundError("saved search", "bad_id")
        service = SearchService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete("bad_id")

        fake_client.delete.assert_not_called()
