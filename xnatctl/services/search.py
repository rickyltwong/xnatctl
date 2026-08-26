"""Saved-search service for XNAT stored searches (``/data/search/saved``).

Every route here was checked against a running XNAT 1.9.2.1 server: the
listing was verified live (real request/response captured), and the
show/run/delete routes were confirmed to be real, registered REST routes --
not a Tomcat 404 for an unmapped path -- via ``javap`` bytecode inspection
of ``org.nrg.xnat.restlet.resources.search.SavedSearchResource`` (which
implements ``represent``/``handlePut``/``handleDelete``) and its
registration in ``XNATApplication``'s route table (``/search/saved/{SEARCH_ID}``
-> ``SavedSearchResource``, ``/search/saved`` -> ``SavedSearchListResource``,
list-only). Show, run, and delete were then each additionally verified live
end to end, by creating one real saved search (``PUT
/data/search/saved/xnatctl_test_search`` with an ``xdat:stored_search`` XML
body), reading it back, running it, and deleting it -- confirmed gone by a
subsequent list and show.

Two shapes are surprising enough to call out loudly:

- ``GET /data/search/saved/{id}`` has NO JSON representation. The same,
  real, existing search answers 200 with its XML definition on the default
  (no ``format``) and explicit ``?format=xml`` requests, but a clean 404 on
  ``?format=json`` -- the identical shape as an unknown id. Every other
  route in this module supports ``format=json``; this one does not, so
  :meth:`SearchService.get_definition` never asks for it.
- ``GET /data/search/saved/{id}/results`` (the "run" route) IS live and
  IS distinct from ``show`` -- confirmed via ``javap``: ``represent()``
  branches on whether the trailing URL segment starts with ``"results"``
  and, in that branch, calls
  ``org.nrg.xft.db.MaterializedView.getViewBySearchID`` to materialize and
  return the search's actual result rows, rather than its definition.
"""

from __future__ import annotations

from typing import Any

from xnatctl.core.validation import quote_path_segment

from .base import BaseService
from .hierarchy import HierarchyService


class SearchService(BaseService):
    """Service for XNAT saved (stored) searches.

    Every method returns a plain ``str``/``list[dict]`` -- a saved search's
    definition is an XML document with no fixed schema xnatctl needs to
    model, and a search's result rows have a dynamic, per-search column
    shape (whatever fields the search was built with), so there is no
    stable Pydantic shape to give either one (per the data-flow rule in
    ``AGENTS.md``).
    """

    def list_searches(self) -> list[dict[str, Any]]:
        """List saved searches.

        Verified live against XNAT 1.9.2.1: ``GET /data/search/saved`` (and
        its explicit ``?format=json``, identical) answers 200 with a
        ``ResultSet`` envelope -- ``{"ResultSet": {"Result": [...], "title":
        "Stored Searches"}}`` -- NOT a bare array. ``/xapi/search/saved``
        does not exist (404); there is no xapi variant of this route.

        Returns:
            List of saved-search row dicts. Verified key set, from a real
            saved search created for this verification: ``id``,
            ``root_element_name``, ``brief_description``, ``description``,
            ``secure``, ``users``, ``tag``, ``sort_by_element_name``,
            ``sort_by_field_id``, ``allow_diff_columns``,
            ``layeredsequence``, ``extension``, ``stored_search_info``.
        """
        data = self.client.get_json("/data/search/saved")
        return HierarchyService.extract_rows_strict(data, "listing saved searches")

    def get_definition(self, search_id: str) -> str:
        """Return a saved search's XML definition.

        Verified live against XNAT 1.9.2.1, against a real saved search
        created for this check: ``GET /data/search/saved/{id}`` (default
        format, and its explicit ``?format=xml``) answers 200 with the
        search's ``xdat:bundle`` XML. ``?format=json`` answers a clean 404
        for that SAME existing search -- this route has no JSON
        representation at all, unlike almost every other read route in this
        codebase -- so this method deliberately never asks for it.

        Args:
            search_id: Saved search ID.

        Returns:
            The raw XML document as returned by the server.

        Raises:
            ResourceNotFoundError: If ``search_id`` does not exist.
        """
        path = f"/data/search/saved/{quote_path_segment(search_id)}"
        return self.client.get(path, params={"format": "xml"}).text

    def run(self, search_id: str) -> list[dict[str, Any]]:
        """Execute a saved search and return its result rows.

        Verified live against XNAT 1.9.2.1, against a real saved search
        created for this check: ``GET
        /data/search/saved/{id}/results?format=json`` answers 200 with a
        ``ResultSet`` envelope whose ``Columns``/``Result`` shape is
        entirely dynamic -- dependent on the fields the search was built
        with. No fixed column set is assumed here; callers render whatever
        keys the rows actually carry (see ``xnatctl.cli.search``'s
        dynamic-column handling, modeled on ``xsync list``'s).

        Args:
            search_id: Saved search ID.

        Returns:
            List of result row dicts, in whatever columns the search defines.

        Raises:
            ResourceNotFoundError: If ``search_id`` does not exist.
        """
        path = f"/data/search/saved/{quote_path_segment(search_id)}/results"
        data = self.client.get_json(path)
        return HierarchyService.extract_rows_strict(data, f"running saved search {search_id!r}")

    def delete(self, search_id: str) -> None:
        """Delete a saved search.

        Verified live against XNAT 1.9.2.1: creating a real saved search,
        deleting it, then re-listing and re-showing it confirmed it gone.
        ``DELETE /data/search/saved/{id}`` also answers 200 for an UNKNOWN
        id -- delete is idempotent-succeeds here, the same shape as
        ``CommandService.delete_command`` -- so, like that method, this
        preflights via :meth:`get_definition` (confirmed live to 404 on an
        unknown id) before deleting, rather than reporting success for a
        search that was never there. An earlier version of this docstring
        claimed that preflight was "not possible"; it is -- the same GET
        this class already exposes for ``search show`` does it.

        Args:
            search_id: Saved search ID.

        Raises:
            ResourceNotFoundError: If ``search_id`` does not exist.
        """
        self.get_definition(search_id)
        path = f"/data/search/saved/{quote_path_segment(search_id)}"
        self.client.delete(path)
