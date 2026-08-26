"""Server-side ``limit`` forwarding for the typed ``list()`` service methods.

``ProjectService.list()``, ``SubjectService.list()``, and
``SessionService.list()`` forward ``limit`` to the server as a request
parameter -- fetching the full result set and slicing it in Python would
pull every row of a site-wide listing just to return ``limit``. The
client-side slice stays as belt-and-braces for endpoints that ignore it.
``limit=0`` must still mean "zero results", not "no limit" -- these tests
pin both ends of that contract for each service.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import make_response

from xnatctl.services.projects import ProjectService
from xnatctl.services.sessions import SessionService
from xnatctl.services.subjects import SubjectService

SAMPLE_PROJECT_ROWS = [{"ID": f"PROJ{i:02d}", "label": f"PROJ{i:02d}"} for i in range(5)]
SAMPLE_SUBJECT_ROWS = [{"ID": f"SUB{i:02d}", "label": f"SUB{i:02d}"} for i in range(5)]
SAMPLE_SESSION_ROWS = [{"ID": f"EXP{i:02d}", "label": f"EXP{i:02d}"} for i in range(5)]


@pytest.fixture
def mock_client() -> MagicMock:
    """A bare mock client.

    Not ``fake_client``'s XNATClient spec -- these services only call
    ``.get()``, and a plain mock keeps ``call_args`` simple.
    """
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


class TestProjectServiceLimit:
    def test_list_with_limit_sends_limit_param(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_PROJECT_ROWS}})
        service = ProjectService(mock_client)

        service.list(limit=10)

        params = mock_client.get.call_args.kwargs["params"]
        assert params["limit"] == "10"

    def test_list_limit_zero_sends_limit_and_yields_nothing(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_PROJECT_ROWS}})
        service = ProjectService(mock_client)

        result = service.list(limit=0)

        params = mock_client.get.call_args.kwargs["params"]
        assert params["limit"] == "0"
        assert result == []

    def test_list_no_limit_does_not_send_limit_param(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_PROJECT_ROWS}})
        service = ProjectService(mock_client)

        service.list()

        params = mock_client.get.call_args.kwargs["params"]
        assert "limit" not in params


class TestSubjectServiceLimit:
    def test_list_with_limit_sends_limit_param(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_SUBJECT_ROWS}})
        service = SubjectService(mock_client)

        service.list(project="PROJ01", limit=3)

        params = mock_client.get.call_args.kwargs["params"]
        assert params["limit"] == "3"

    def test_list_limit_zero_sends_limit_and_yields_nothing(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_SUBJECT_ROWS}})
        service = SubjectService(mock_client)

        result = service.list(project="PROJ01", limit=0)

        params = mock_client.get.call_args.kwargs["params"]
        assert params["limit"] == "0"
        assert result == []


class TestSessionServiceLimit:
    def test_list_with_limit_sends_limit_param(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_SESSION_ROWS}})
        service = SessionService(mock_client)

        service.list(project="PROJ01", limit=2)

        params = mock_client.get.call_args.kwargs["params"]
        assert params["limit"] == "2"

    def test_list_limit_zero_sends_limit_and_yields_nothing(self, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": SAMPLE_SESSION_ROWS}})
        service = SessionService(mock_client)

        result = service.list(project="PROJ01", limit=0)

        params = mock_client.get.call_args.kwargs["params"]
        assert params["limit"] == "0"
        assert result == []
