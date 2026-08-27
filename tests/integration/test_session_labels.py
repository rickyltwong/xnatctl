"""Live-server proof for ``session normalize-labels``.

Creates two MR experiments on different dates for one subject in a project
of its OWN, runs the label-normalization plan/apply cycle against the real
XNAT, and asserts both the computed plan and the labels XNAT actually
stores afterward.

The dedicated project is load-bearing, not tidiness: normalization plans
over an ENTIRE project, and the shared ``label_project`` fixture is
also written to by the golden-path, sharing, anon, prearchive and container
suites. Asserting a whole-project rename count against it passes alone and
fails in a full run, because somebody else's experiment is in the plan too.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def label_project(xnat_client: Any) -> Iterator[str]:
    """A project used by this file alone, deleted afterward.

    See the module docstring: this test asserts on a whole-project plan, so
    it cannot share a project with anything else that creates experiments.
    """
    project_id = f"xctl{uuid.uuid4().hex[:12]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except Exception as exc:  # noqa: BLE001  # teardown must not mask a test result
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")


class TestSessionNormalizeLabels:
    def test_normalizes_two_visits_for_one_subject(
        self, xnat_client: Any, label_project: str
    ) -> None:
        from xnatctl.services.session_labels import SessionLabelService

        subject = "NORMSUBJ"
        xnat_client.put(f"/data/projects/{label_project}/subjects/{subject}")

        exp1 = xnat_client.put(
            f"/data/projects/{label_project}/subjects/{subject}/experiments/RAW1",
            params={"xsiType": "xnat:mrSessionData", "date": "2024-01-01"},
        ).text.strip()
        exp2 = xnat_client.put(
            f"/data/projects/{label_project}/subjects/{subject}/experiments/RAW2",
            params={"xsiType": "xnat:mrSessionData", "date": "2024-02-01"},
        ).text.strip()

        service = SessionLabelService(xnat_client)
        plan = service.plan_label_normalization(label_project)

        by_id = {r["id"]: r["new_label"] for r in plan["renames"]}
        assert by_id[exp1] == f"{subject}_01_SE01_MR"
        assert by_id[exp2] == f"{subject}_02_SE01_MR"
        assert plan["skipped"] == []

        result = service.apply_label_normalization(plan)
        assert result["failed"] == []
        assert result["renamed"] == 2

        rows = xnat_client.get_json(
            f"/data/projects/{label_project}/experiments",
            params={"columns": "ID,label"},
        )
        labels = {r["ID"]: r["label"] for r in rows.get("ResultSet", {}).get("Result", [])}
        assert labels[exp1] == f"{subject}_01_SE01_MR"
        assert labels[exp2] == f"{subject}_02_SE01_MR"

        # Re-planning against the now-normalized labels finds nothing left to do.
        replan = service.plan_label_normalization(label_project)
        assert replan["renames"] == []
