from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, ServerError
from xnatctl.services.base import BaseService
from xnatctl.services.pipelines import PipelineService
from xnatctl.services.projects import ProjectService
from xnatctl.services.scans import ScanService
from xnatctl.services.sessions import SessionService
from xnatctl.services.subjects import SubjectService


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


_GET_CASES = [
    pytest.param(
        SubjectService,
        lambda svc: svc.get("MISSING"),
        "subject",
        "MISSING",
        id="subjects",
    ),
    pytest.param(
        SessionService,
        lambda svc: svc.get("MISSING"),
        "session",
        "MISSING",
        id="sessions",
    ),
    pytest.param(
        ProjectService,
        lambda svc: svc.get("MISSING"),
        "project",
        "MISSING",
        id="projects",
    ),
    pytest.param(
        ScanService,
        lambda svc: svc.get("SESSION1", "MISSING"),
        "scan",
        "SESSION1/MISSING",
        id="scans",
    ),
    pytest.param(
        PipelineService,
        lambda svc: svc.get("MISSING"),
        "pipeline",
        "MISSING",
        id="pipelines",
    ),
]


@pytest.mark.parametrize("service_cls,call,resource_type,resource_id", _GET_CASES)
def test_typed_404_dispatches_on_type_not_message_text(
    service_cls: type[BaseService],
    call: Callable[[Any], Any],
    resource_type: str,
    resource_id: str,
) -> None:
    client = _mock_client()
    original = ResourceNotFoundError("resource", "no such thing here")
    client.get.side_effect = original
    service = service_cls(client)

    with pytest.raises(ResourceNotFoundError) as excinfo:
        call(service)

    err = excinfo.value
    assert err is not original
    assert err.__cause__ is original
    assert err.details.get("resource_type") == resource_type
    assert err.details.get("resource_id") == resource_id


_FALSE_POSITIVE_CASES = [
    pytest.param(
        SubjectService, lambda svc: svc.get("SUB404"), "/data/subjects/SUB404", id="subjects"
    ),
    pytest.param(
        SessionService,
        lambda svc: svc.get("SESSION404"),
        "/data/experiments/SESSION404",
        id="sessions",
    ),
    pytest.param(
        ProjectService, lambda svc: svc.get("PROJ404"), "/data/projects/PROJ404", id="projects"
    ),
    pytest.param(
        ScanService,
        lambda svc: svc.get("SESSION1", "SCAN404"),
        "/data/experiments/SESSION1/scans/SCAN404",
        id="scans",
    ),
    pytest.param(
        PipelineService,
        lambda svc: svc.get("PIPE404"),
        "/data/pipelines/PIPE404",
        id="pipelines",
    ),
]


@pytest.mark.parametrize("service_cls,call,path", _FALSE_POSITIVE_CASES)
def test_non_404_error_whose_text_contains_404_propagates_unchanged(
    service_cls: type[BaseService],
    call: Callable[[Any], Any],
    path: str,
) -> None:
    client = _mock_client()
    original = ServerError(500, "GET", path, "")
    client.get.side_effect = original
    service = service_cls(client)

    with pytest.raises(ServerError) as excinfo:
        call(service)

    assert excinfo.value is original
