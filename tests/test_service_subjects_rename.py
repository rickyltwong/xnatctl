from __future__ import annotations

from unittest.mock import MagicMock

from conftest import make_response

from xnatctl.core.exceptions import ServerError
from xnatctl.services.subjects import SubjectService


def _service(rows: list[dict[str, str]]) -> tuple[SubjectService, MagicMock]:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    client.get.return_value = make_response({"ResultSet": {"Result": rows}})
    return SubjectService(client), client


def test_a_failed_rename_is_recorded_and_the_next_one_still_runs() -> None:
    service, client = _service([{"ID": "S1", "label": "SUB1_v1"}, {"ID": "S2", "label": "SUB2_v1"}])
    client.put.side_effect = [ServerError(500, "PUT", "/p", "boom"), make_response({})]

    result = service.rename_pattern("P1", r"^(SUB\d)_v1$", "{1}")

    assert result["errors"] == [{"label": "SUB1_v1", "error": "HTTP 500 on PUT /p"}]
    assert result["renamed"] == [{"from": "SUB2_v1", "to": "SUB2"}]


def test_merge_renames_every_subject_into_the_shared_label() -> None:
    service, client = _service([{"ID": "S1", "label": "A_v1"}, {"ID": "S2", "label": "A_v2"}])
    client.put.return_value = make_response({})

    result = service.rename_pattern("P1", r"^(A)_v\d$", "{1}", merge=True)

    assert result["merged"] == [{"from": "A_v1", "to": "A"}, {"from": "A_v2", "to": "A"}]
    assert result["errors"] == []
    assert client.put.call_count == 2
