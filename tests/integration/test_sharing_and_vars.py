"""Cross-project sharing and custom-variable get/set against a real XNAT.

Every route these two commands use was verified live and is documented on
``SubjectService``/``SessionService``'s ``share``/``unshare``/``list_vars``/
``set_vars`` docstrings. This tier proves the same thing against a fresh
server run, not just the request shapes the unit suite asserts.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def second_project(xnat_client: Any) -> Any:
    """A second throwaway project, distinct from ``integration_project``, deleted afterwards."""
    project_id = f"xctl{uuid.uuid4().hex[:12]}"
    xnat_client.put(f"/data/projects/{project_id}")
    try:
        yield project_id
    finally:
        try:
            xnat_client.delete(f"/data/projects/{project_id}", params={"removeFiles": "true"})
        except Exception as exc:  # noqa: BLE001  # teardown must not mask a test result
            print(f"\nWARNING: could not delete test project {project_id}: {exc}")


class TestSubjectSharing:
    def test_share_then_unshare_a_subject_between_two_projects(
        self, xnat_client: Any, integration_project: str, second_project: str
    ) -> None:
        from xnatctl.services.hierarchy import HierarchyService
        from xnatctl.services.subjects import SubjectService

        label = "SHARESUBJ"
        xnat_client.put(f"/data/projects/{integration_project}/subjects/{label}")
        hierarchy = HierarchyService(xnat_client)
        subject_service = SubjectService(xnat_client)

        from xnatctl.models.hierarchy import SubjectRef

        resolved = hierarchy.resolve_subject(
            SubjectRef(subject=label, project_id=integration_project)
        )

        # Not shared yet.
        shares_before = {r["ID"] for r in subject_service.list_shares(resolved.subject_id)}
        assert second_project not in shares_before

        subject_service.share(resolved.subject_id, second_project, label="SHARED_LABEL")

        shares_after = {
            r["ID"]: r["label"] for r in subject_service.list_shares(resolved.subject_id)
        }
        assert shares_after[second_project] == "SHARED_LABEL"

        # The shared project's subject listing now includes it.
        shared_ids = {s.id for s in subject_service.list(second_project)}
        assert resolved.subject_id in shared_ids

        subject_service.unshare(
            resolved.subject_id, second_project, primary_project=integration_project
        )

        shares_final = {r["ID"] for r in subject_service.list_shares(resolved.subject_id)}
        assert second_project not in shares_final

    def test_share_conflict_raises_resource_exists(
        self, xnat_client: Any, integration_project: str, second_project: str
    ) -> None:
        from xnatctl.core.exceptions import ResourceExistsError
        from xnatctl.services.hierarchy import HierarchyService
        from xnatctl.services.subjects import SubjectService

        label = "SHARECONFLICT"
        xnat_client.put(f"/data/projects/{integration_project}/subjects/{label}")
        hierarchy = HierarchyService(xnat_client)
        subject_service = SubjectService(xnat_client)

        from xnatctl.models.hierarchy import SubjectRef

        resolved = hierarchy.resolve_subject(
            SubjectRef(subject=label, project_id=integration_project)
        )

        subject_service.share(resolved.subject_id, second_project)
        try:
            with pytest.raises(ResourceExistsError):
                subject_service.share(resolved.subject_id, second_project)
        finally:
            subject_service.unshare(
                resolved.subject_id, second_project, primary_project=integration_project
            )


class TestSessionSharing:
    def test_share_then_unshare_a_session_between_two_projects(
        self, xnat_client: Any, integration_project: str, second_project: str
    ) -> None:
        from xnatctl.services.sessions import SessionService

        subject = "SESSSHARESUBJ"
        session_label = "SESSSHARE01"
        xnat_client.put(f"/data/projects/{integration_project}/subjects/{subject}")
        resp = xnat_client.put(
            f"/data/projects/{integration_project}/subjects/{subject}/experiments/{session_label}",
            params={"xsiType": "xnat:mrSessionData"},
        )
        experiment_id = resp.text.strip()

        session_service = SessionService(xnat_client)
        shares_before = {r["ID"] for r in session_service.list_shares(experiment_id)}
        assert second_project not in shares_before

        session_service.share(experiment_id, second_project, label="SESS_SHARED")

        shares_after = {r["ID"]: r["label"] for r in session_service.list_shares(experiment_id)}
        assert shares_after[second_project] == "SESS_SHARED"

        # Idempotent unshare: works once, and succeeds again (verified live
        # to differ from the subject case -- see SessionService.unshare).
        session_service.unshare(experiment_id, second_project, primary_project=integration_project)
        session_service.unshare(experiment_id, second_project, primary_project=integration_project)

        shares_final = {r["ID"] for r in session_service.list_shares(experiment_id)}
        assert second_project not in shares_final


class TestSubjectCustomVars:
    def test_set_and_read_back_custom_variables(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        from xnatctl.services.subjects import SubjectService

        label = "VARSUBJ"
        xnat_client.put(f"/data/projects/{integration_project}/subjects/{label}")
        service = SubjectService(xnat_client)

        assert service.list_vars(label, project=integration_project) == []

        service.set_vars(label, {"studytag": "phase1", "cohort": "A"}, project=integration_project)

        rows = {
            r["name"]: r["value"] for r in service.list_vars(label, project=integration_project)
        }
        assert rows == {"studytag": "phase1", "cohort": "A"}

        # Overwrite an existing variable in a second call.
        service.set_vars(label, {"studytag": "phase2"}, project=integration_project)
        rows2 = {
            r["name"]: r["value"] for r in service.list_vars(label, project=integration_project)
        }
        assert rows2["studytag"] == "phase2"
        assert rows2["cohort"] == "A"


class TestSessionCustomVars:
    def test_set_and_read_back_custom_variables(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        from xnatctl.services.hierarchy import HierarchyService
        from xnatctl.services.sessions import SessionService

        subject = "VARSESSSUBJ"
        session_label = "VARSESS01"
        xnat_client.put(f"/data/projects/{integration_project}/subjects/{subject}")
        resp = xnat_client.put(
            f"/data/projects/{integration_project}/subjects/{subject}/experiments/{session_label}",
            params={"xsiType": "xnat:mrSessionData"},
        )
        experiment_id = resp.text.strip()

        session_service = SessionService(xnat_client)
        hierarchy = HierarchyService(xnat_client)

        from xnatctl.models.hierarchy import ExperimentRef

        resolved = hierarchy.resolve_experiment(
            ExperimentRef(experiment=experiment_id, project_id=integration_project)
        )
        assert resolved.project_id
        assert resolved.subject_id
        assert resolved.xsi_type

        assert session_service.list_vars(experiment_id) == []

        # Regression proof for the silent-no-op trap documented on
        # SessionService.set_vars: the flat experiment PUT would return 200
        # without persisting anything, so this must go through the
        # subject-scoped path with the xsiType-prefixed field key.
        session_service.set_vars(
            project=resolved.project_id,
            subject=resolved.subject_id,
            experiment_id=experiment_id,
            xsi_type=resolved.xsi_type,
            fields={"studytag": "phase1"},
        )

        rows = {r["name"]: r["value"] for r in session_service.list_vars(experiment_id)}
        assert rows == {"studytag": "phase1"}
