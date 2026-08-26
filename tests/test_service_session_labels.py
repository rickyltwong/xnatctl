"""Tests for SessionLabelService: label computation and rename application."""

from __future__ import annotations

import pytest

from xnatctl.core.exceptions import SessionExpiredError, XNATCtlError
from xnatctl.services.session_labels import SessionLabelService, build_experiment_label


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
        assert by_id["E2"] == "SUB01_01_SE01_MR"  # earlier date -> visit 1
        assert by_id["E1"] == "SUB01_02_SE01_MR"  # later date -> visit 2
        assert plan["skipped"] == []

    def test_same_day_ordered_by_time(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01", time="14:00:00"),
            _row("E2", "OLD2", date="2024-01-01", time="09:00:00"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        by_id = {r["id"]: r["new_label"] for r in plan["renames"]}
        assert by_id["E2"] == "SUB01_01_SE01_MR"  # earlier time -> session 1
        assert by_id["E1"] == "SUB01_01_SE02_MR"  # later time -> session 2

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

    def test_target_collides_with_another_experiments_current_label(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01"),
            _row("E2", "SUB01_01_SE01_MR", date="2024-02-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        # E1's computed target (SUB01_01_SE01_MR) is E2's current label.
        by_id_skip = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "target label exists" in by_id_skip["E1"]

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
        # A 200 with no 'ResultSet' key at all (e.g. a plugin-disabled body)
        # must not be coerced into "no experiments" -- before the fix,
        # ResultSetEnvelope's default_factory silently validated this to an
        # empty plan, so `session normalize-labels` reported "Renamed 0"
        # instead of surfacing the server's actual, non-ResultSet response.
        fake_client.get_json.return_value = {"message": "plugin disabled"}
        service = SessionLabelService(fake_client)

        with pytest.raises(XNATCtlError, match="ResultSet"):
            service.plan_label_normalization("PROJ")

    def test_genuinely_empty_result_set_is_not_an_error(self, fake_client) -> None:
        # Guard against over-correcting: a 'ResultSet' key that is present
        # but genuinely empty (a project with no experiments) is a normal,
        # legitimate response and must keep returning an empty plan, not
        # raise.
        fake_client.get_json.return_value = {"ResultSet": {"Result": []}}
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan == {"renames": [], "skipped": []}


class TestApplyLabelNormalization:
    def test_applies_each_rename_via_put(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        plan = {
            "renames": [
                {"id": "E1", "subject": "SUB01", "old_label": "OLD1", "new_label": "NEW1"},
                {"id": "E2", "subject": "SUB01", "old_label": "OLD2", "new_label": "NEW2"},
            ]
        }

        result = service.apply_label_normalization(plan)

        assert result == {"renamed": 2, "failed": []}
        assert fake_client.put.call_count == 2
        fake_client.put.assert_any_call("/data/experiments/E1", params={"label": "NEW1"})
        fake_client.put.assert_any_call("/data/experiments/E2", params={"label": "NEW2"})

    def test_per_item_failure_isolation(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        plan = {
            "renames": [
                {"id": "E1", "subject": "SUB01", "old_label": "OLD1", "new_label": "NEW1"},
                {"id": "E2", "subject": "SUB01", "old_label": "OLD2", "new_label": "NEW2"},
            ]
        }
        fake_client.put.side_effect = [RuntimeError("boom"), None]

        result = service.apply_label_normalization(plan)

        assert result["renamed"] == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "E1"
        assert "boom" in result["failed"][0]["error"]

    def test_session_expired_aborts_rather_than_isolated(self, fake_client) -> None:
        service = SessionLabelService(fake_client)
        plan = {
            "renames": [
                {"id": "E1", "subject": "SUB01", "old_label": "OLD1", "new_label": "NEW1"},
            ]
        }
        fake_client.put.side_effect = SessionExpiredError("https://example.org")

        try:
            service.apply_label_normalization(plan)
        except SessionExpiredError:
            pass
        else:
            raise AssertionError("expected SessionExpiredError to propagate")
