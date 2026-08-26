"""Saved (stored) searches, checked against a real server.

Every claim SearchService depends on was verified once, by hand, against
this same stack: the listing route returns a ``ResultSet`` envelope, the
definition route (``show``) has NO JSON representation at all (``?format=json``
404s even for a real, existing search -- only the default/``?format=xml``
form works), the run route (``.../results``) supports JSON and returns a
dynamic ``ResultSet.Columns``/``Result`` shape, and delete is
idempotent-succeeds (a 200 even for an unknown id). This file is what keeps
that verification from going stale.

xnatctl ships no ``search create`` -- creating a saved search server-side
has no clean single-call REST route (its ``PUT`` needs a full
``xdat:stored_search`` XML bundle, the same shape the web UI's search
builder produces) -- so this file builds one directly via the raw client,
the same way the CLI's own escape hatch (``xnatctl api put``) would have to.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.services.search import SearchService

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

_STORED_SEARCH_XML = """<xdat:stored_search ID="{search_id}" allow-diff-columns="0" \
brief-description="xnatctl integration test search" root_element_name="xnat:projectData" \
sort-by-element-name="xnat:projectData" xmlns:xdat="http://nrg.wustl.edu/security">
  <xdat:search_field>
    <xdat:element_name>xnat:projectData</xdat:element_name>
    <xdat:field_ID>ID</xdat:field_ID>
    <xdat:sequence>0</xdat:sequence>
    <xdat:type>string</xdat:type>
    <xdat:header>ID</xdat:header>
  </xdat:search_field>
</xdat:stored_search>"""


@pytest.fixture
def saved_search(xnat_client: Any) -> Iterator[str]:
    """A real saved search, created directly via PUT, deleted afterward."""
    search_id = f"xctlsrch{uuid.uuid4().hex[:10]}"
    xnat_client.put(
        f"/data/search/saved/{search_id}",
        params={"format": "xml"},
        content=_STORED_SEARCH_XML.format(search_id=search_id).encode("utf-8"),
        headers={"Content-Type": "application/xml"},
    )
    try:
        yield search_id
    finally:
        try:
            xnat_client.delete(f"/data/search/saved/{search_id}")
        except Exception as exc:  # noqa: BLE001 -- teardown must not mask a test result
            print(f"\nWARNING: could not delete test saved search {search_id}: {exc}")


class TestListSearches:
    """List saved searches against the real server."""

    def test_created_search_appears_in_listing(self, xnat_client: Any, saved_search: str) -> None:
        service = SearchService(xnat_client)

        rows = service.list_searches()

        assert any(row.get("id") == saved_search for row in rows)


class TestGetDefinition:
    """Show a real saved search's XML definition."""

    def test_get_definition_returns_xml(self, xnat_client: Any, saved_search: str) -> None:
        service = SearchService(xnat_client)

        definition_xml = service.get_definition(saved_search)

        assert saved_search in definition_xml
        assert "xnat:projectData" in definition_xml


class TestRun:
    """Execute a real saved search."""

    def test_run_returns_a_list(self, xnat_client: Any, saved_search: str) -> None:
        service = SearchService(xnat_client)

        rows = service.run(saved_search)

        assert isinstance(rows, list)


class TestDelete:
    """Delete, including the preflight that rejects an unknown id.

    XNAT's own ``DELETE /data/search/saved/{id}`` is idempotent: it answers
    success for an id that never existed, so a typo would print "Deleted
    saved search X" having deleted nothing. ``delete()`` reads the
    definition first and fails on an unknown id; the assertions below pin
    the client-side behaviour, not the server's.
    """

    def test_delete_removes_a_real_search(self, xnat_client: Any) -> None:
        service = SearchService(xnat_client)
        search_id = f"xctlsrch{uuid.uuid4().hex[:10]}"
        xnat_client.put(
            f"/data/search/saved/{search_id}",
            params={"format": "xml"},
            content=_STORED_SEARCH_XML.format(search_id=search_id).encode("utf-8"),
            headers={"Content-Type": "application/xml"},
        )

        service.delete(search_id)

        rows = service.list_searches()
        assert not any(row.get("id") == search_id for row in rows)

    def test_delete_unknown_search_id_is_refused(self, xnat_client: Any) -> None:
        service = SearchService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete(f"xctlsrch-does-not-exist-{uuid.uuid4().hex[:10]}")
