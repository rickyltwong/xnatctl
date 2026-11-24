from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import (
    NetworkError,
    RetryExhaustedError,
    SessionExpiredError,
    XNATCtlError,
)
from xnatctl.services import session_labels
from xnatctl.services.session_labels import SessionLabelService, build_experiment_label


def wire_rename_confirmation(
    fake_client: MagicMock,
    *,
    status: str = "Complete",
    put: Callable[..., None] | None = None,
) -> dict[str, int]:
    landed: dict[str, int] = {}

    def default_put(path: str, **_: object) -> None:
        landed[path.rsplit("/", 1)[1]] = 100 + len(landed)

    def get_json(path: str, **_: object) -> object:
        exp_id = path.split("/")[3]
        event = landed.get(exp_id)
        events = (
            [{"event_action": "Renamed", "event_id": event, "event_status": status}]
            if event
            else []
        )
        return {"events": events}

    fake_client.put.side_effect = put or default_put
    fake_client.get_json.side_effect = get_json
    return landed


def _plan(*ids: str) -> dict:
    return {
        "renames": [
            {"id": i, "subject": "SUB01", "old_label": f"OLD{i}", "new_label": f"NEW{i}"}
            for i in ids
        ]
    }


def _row(
    exp_id: str,
    label: str,
    *,
    subject: str = "SUB01",
    xsi_type: str = "xnat:mrsessiondata",
    date: str = "2024-01-01",
    time: str = "",
    insert_date: str = "",
    insert_time: str = "",
) -> dict:
    return {
        "ID": exp_id,
        "label": label,
        "subject_label": subject,
        "xsiType": xsi_type,
        "date": date,
        "time": time,
        "insert_date": insert_date,
        "insert_time": insert_time,
    }


class TestBuildExperimentLabel:
    def test_formats_visit_and_session_zero_padded(self) -> None:
        assert build_experiment_label("PROJ_SUB01", 1, 2, "MR") == "PROJ_SUB01_01_SE02_MR"

    def test_double_digit_indices(self) -> None:
        assert build_experiment_label("SUB", 11, 12, "PET") == "SUB_11_SE12_PET"


class TestPlanLabelNormalization:
    def test_two_visits_ordered_by_date(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-02-01"),
            _row("E2", "OLD2", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        by_id = {r["id"]: r["new_label"] for r in plan["renames"]}
        assert by_id["E2"] == "SUB01_01_SE01_MR"
        assert by_id["E1"] == "SUB01_02_SE01_MR"
        assert plan["skipped"] == []

    def test_same_day_ordered_by_time(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01", time="14:00:00"),
            _row("E2", "OLD2", date="2024-01-01", time="09:00:00"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        by_id = {r["id"]: r["new_label"] for r in plan["renames"]}
        assert by_id["E2"] == "SUB01_01_SE01_MR"
        assert by_id["E1"] == "SUB01_01_SE02_MR"

    def test_same_day_missing_time_both_skipped(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01"),
            _row("E2", "OLD2", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        reasons = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "cannot assign SE order" in reasons["E1"]
        assert "cannot assign SE order" in reasons["E2"]

    def test_already_correct_label_is_a_no_op(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "SUB01_01_SE01_MR", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"] == []

    def test_target_held_by_a_non_renaming_experiment_is_refused(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01"),
            _row("E2", "SUB01_01_SE01_MR", xsi_type="xnat:imagesessiondata"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        by_id_skip = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "target label exists" in by_id_skip["E1"]
        assert plan["renames"] == []

    def test_target_vacated_by_an_earlier_planned_rename_is_allowed(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01"),
            _row("E2", "SUB01_01_SE01_MR", date="2024-02-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["skipped"] == []
        assert [(r["id"], r["new_label"]) for r in plan["renames"]] == [
            ("E2", "SUB01_02_SE01_MR"),
            ("E1", "SUB01_01_SE01_MR"),
        ]

    def test_a_rename_cycle_is_refused(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "SUB01_02_SE01_MR", date="2024-01-01"),
            _row("E2", "SUB01_01_SE01_MR", date="2024-02-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        reasons = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "target label exists" in reasons["E1"]
        assert "target label exists" in reasons["E2"]

    def test_a_cross_subject_vacating_chain_resolves_in_order(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", subject="SUB01", date="2024-01-01"),
            _row("E2", "SUB01_01_SE01_MR", subject="SUB02", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["skipped"] == []
        assert [(r["id"], r["new_label"]) for r in plan["renames"]] == [
            ("E2", "SUB02_01_SE01_MR"),
            ("E1", "SUB01_01_SE01_MR"),
        ]

    def test_a_label_held_outside_the_planned_subjects_still_blocks(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", subject="SUB01", date="2024-01-01"),
            _row("E2", "SUB01_01_SE01_MR", subject="SUB02", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ", subjects=["SUB01"])

        assert plan["renames"] == []
        assert plan["skipped"] == [
            {"id": "E1", "label": "OLD1", "reason": "target label exists: SUB01_01_SE01_MR"}
        ]

    def test_a_long_vacating_chain_resolves_without_recursion_error(self, fake_client) -> None:
        from datetime import date as date_cls
        from datetime import timedelta

        n = 1500
        base = date_cls(2000, 1, 1)
        fake_client.get_json.return_value = [
            _row(
                f"E{k}",
                "OLD1" if k == 1 else f"SUB01_{k - 1:02d}_SE01_MR",
                date=str(base + timedelta(days=k)),
            )
            for k in range(1, n + 1)
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["skipped"] == []
        assert len(plan["renames"]) == n
        assert plan["renames"][0]["id"] == f"E{n}"
        assert plan["renames"][-1]["id"] == "E1"

    def test_a_label_held_by_multiple_experiments_is_never_treated_as_vacated(
        self, fake_client
    ) -> None:
        contested = "SUB01_01_SE01_MR"
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", subject="SUB01", date="2024-01-01"),
            _row("E2", contested, subject="SUB02", date="2024-01-01"),
            _row("E3", contested, subject="SUB03", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        planned = {r["id"] for r in plan["renames"]}
        assert planned == {"E2", "E3"}
        assert plan["skipped"] == [
            {
                "id": "E1",
                "label": "OLD1",
                "reason": f"target label exists: {contested} (held by multiple experiments)",
            }
        ]

    def test_a_null_id_row_is_skipped_not_planned(self, fake_client) -> None:
        row = _row("ignored", "OLD1")
        row["ID"] = None
        fake_client.get_json.return_value = [row]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"] == [{"id": "", "label": "OLD1", "reason": "missing experiment ID"}]

    def test_row_missing_experiment_id_is_skipped_not_planned(self, fake_client) -> None:
        fake_client.get_json.return_value = [_row("", "OLD1")]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"] == [{"id": "", "label": "OLD1", "reason": "missing experiment ID"}]

    def test_row_missing_subject_label_is_reported_not_dropped(self, fake_client) -> None:
        fake_client.get_json.return_value = [_row("E1", "OLD1", subject="")]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"] == [
            {"id": "E1", "label": "OLD1", "reason": "row missing subject_label"}
        ]

    def test_bare_array_of_non_objects_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = ["plugin disabled"]
        service = SessionLabelService(fake_client)

        with pytest.raises(XNATCtlError, match="non-object"):
            service.plan_label_normalization("PROJ")

    def test_unknown_modality_is_skipped(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", xsi_type="xnat:imagesessiondata"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"][0]["reason"] == "unknown modality from xsiType"

    def test_missing_date_is_skipped(self, fake_client) -> None:
        fake_client.get_json.return_value = [_row("E1", "OLD1", date="")]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"][0]["reason"] == "missing session date"

    def test_modality_filter_excludes_non_matching(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", xsi_type="xnat:mrsessiondata"),
            _row("E2", "OLD2", xsi_type="xnat:petsessiondata"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ", modalities=["MR"])

        renamed_ids = {r["id"] for r in plan["renames"]}
        assert renamed_ids == {"E1"}
        skipped_reasons = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "PET" in skipped_reasons["E2"]

    def test_subject_filter(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", subject="SUB01"),
            _row("E2", "OLD2", subject="SUB02"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ", subjects=["SUB01"])

        renamed_subjects = {r["subject"] for r in plan["renames"]}
        assert renamed_subjects == {"SUB01"}

    def test_malformed_body_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {"message": "plugin disabled"}
        service = SessionLabelService(fake_client)

        with pytest.raises(XNATCtlError, match="ResultSet"):
            service.plan_label_normalization("PROJ")

    def test_genuinely_empty_result_set_is_not_an_error(self, fake_client) -> None:
        fake_client.get_json.return_value = {"ResultSet": {"Result": []}}
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan == {"renames": [], "skipped": []}


class TestApplyLabelNormalization:
    def test_applies_each_rename_via_put_and_confirms_via_history(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        wire_rename_confirmation(fake_client)

        result = service.apply_label_normalization(_plan("E1", "E2"), poll_interval=0)

        assert result == {"renamed": 2, "failed": []}
        assert fake_client.put.call_count == 2
        fake_client.put.assert_any_call(
            "/data/experiments/E1", params={"label": "NEWE1"}, timeout=60, max_retries=0
        )
        fake_client.put.assert_any_call(
            "/data/experiments/E2", params={"label": "NEWE2"}, timeout=60, max_retries=0
        )
        fake_client.get_json.assert_any_call("/data/experiments/E1/history")
        fake_client.get_json.assert_any_call("/data/experiments/E2/history")

    def test_put_timeout_still_counts_when_rename_completes(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        landed = wire_rename_confirmation(fake_client)

        def put_then_time_out(path: str, **_: object) -> None:
            landed["E1"] = 100
            raise RetryExhaustedError("request", 1, NetworkError("https://x", "Timeout after 60s"))

        fake_client.put.side_effect = put_then_time_out

        result = service.apply_label_normalization(_plan("E1"), poll_interval=0)

        assert result == {"renamed": 1, "failed": []}
        assert fake_client.put.call_count == 1

    def test_put_error_with_no_rename_event_reports_the_put_error(
        self, fake_client, monkeypatch
    ) -> None:
        monkeypatch.setattr(session_labels, "RENAME_WORKFLOW_APPEAR_SECONDS", 0.0)
        service = SessionLabelService(fake_client)
        wire_rename_confirmation(fake_client, put=None)
        fake_client.put.side_effect = NetworkError("https://x", "connection reset")

        result = service.apply_label_normalization(_plan("E1"), poll_interval=0)

        assert result["renamed"] == 0
        assert [f["id"] for f in result["failed"]] == ["E1"]
        assert "connection reset" in result["failed"][0]["error"]

    def test_clean_put_with_no_rename_event_is_a_failure(self, fake_client, monkeypatch) -> None:
        monkeypatch.setattr(session_labels, "RENAME_WORKFLOW_APPEAR_SECONDS", 0.0)
        service = SessionLabelService(fake_client)
        wire_rename_confirmation(fake_client, put=lambda path, **_: None)

        result = service.apply_label_normalization(_plan("E1"), poll_interval=0)

        assert result["renamed"] == 0
        assert "no rename event appeared" in result["failed"][0]["error"]

    def test_failed_rename_is_reported(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        wire_rename_confirmation(fake_client, status="Failed")

        result = service.apply_label_normalization(_plan("E1"), poll_interval=0)

        assert result["renamed"] == 0
        assert "ended 'failed'" in result["failed"][0]["error"]

    def test_rename_still_running_at_timeout_is_reported(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        wire_rename_confirmation(fake_client, status="In Progress")

        result = service.apply_label_normalization(_plan("E1"), rename_timeout=0, poll_interval=0)

        assert result["renamed"] == 0
        assert "in progress after 0s" in result["failed"][0]["error"]

    def test_per_item_failure_isolation(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        landed = wire_rename_confirmation(fake_client)

        def put(path: str, **_: object) -> None:
            if path.endswith("/E1"):
                raise RuntimeError("boom")
            landed["E2"] = 100

        fake_client.put.side_effect = put

        result = service.apply_label_normalization(_plan("E1", "E2"), poll_interval=0)

        assert result["renamed"] == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "E1"
        assert "boom" in result["failed"][0]["error"]

    def test_session_expired_aborts_rather_than_isolated(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        wire_rename_confirmation(fake_client)
        fake_client.put.side_effect = SessionExpiredError("https://example.org")

        with pytest.raises(SessionExpiredError):
            service.apply_label_normalization(_plan("E1"), poll_interval=0)
