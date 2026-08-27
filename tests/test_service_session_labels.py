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

    def test_target_held_by_a_non_renaming_experiment_is_refused(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01"),
            # Holds E1's computed target but is itself skipped (unknown
            # modality), so the label never frees up.
            _row("E2", "SUB01_01_SE01_MR", xsi_type="xnat:imagesessiondata"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        by_id_skip = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "target label exists" in by_id_skip["E1"]
        assert plan["renames"] == []

    def test_target_vacated_by_an_earlier_planned_rename_is_allowed(self, fake_client) -> None:
        """A collision with a label this run renames away resolves, vacator first.

        Treating every current label as permanently occupied would skip E1
        here and leave the subject half-normalized until a second run.
        """
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", date="2024-01-01"),
            # Currently holds E1's target, but its own rename (visit 2)
            # vacates it.
            _row("E2", "SUB01_01_SE01_MR", date="2024-02-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["skipped"] == []
        assert [(r["id"], r["new_label"]) for r in plan["renames"]] == [
            ("E2", "SUB01_02_SE01_MR"),  # vacates SUB01_01_SE01_MR first
            ("E1", "SUB01_01_SE01_MR"),
        ]

    def test_a_rename_cycle_is_refused(self, fake_client) -> None:
        """Two experiments swapping labels have no safe order without a temp name."""
        fake_client.get_json.return_value = [
            _row("E1", "SUB01_02_SE01_MR", date="2024-01-01"),  # target: .._01_SE01_MR
            _row("E2", "SUB01_01_SE01_MR", date="2024-02-01"),  # target: .._02_SE01_MR
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        reasons = {s["id"]: s["reason"] for s in plan["skipped"]}
        assert "target label exists" in reasons["E1"]
        assert "target label exists" in reasons["E2"]

    def test_a_cross_subject_vacating_chain_resolves_in_order(self, fake_client) -> None:
        """Labels are project-unique, so the vacator may belong to another subject."""
        fake_client.get_json.return_value = [
            _row("E1", "OLD1", subject="SUB01", date="2024-01-01"),
            # A DIFFERENT subject currently holds SUB01's target and
            # vacates it via its own rename.
            _row("E2", "SUB01_01_SE01_MR", subject="SUB02", date="2024-01-01"),
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["skipped"] == []
        assert [(r["id"], r["new_label"]) for r in plan["renames"]] == [
            ("E2", "SUB02_01_SE01_MR"),  # vacates SUB01_01_SE01_MR first
            ("E1", "SUB01_01_SE01_MR"),
        ]

    def test_a_label_held_outside_the_planned_subjects_still_blocks(self, fake_client) -> None:
        """A subject excluded by the filter never renames, so its labels are occupied."""
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
        """A valid chain longer than the interpreter's recursion limit must plan."""
        from datetime import date as date_cls
        from datetime import timedelta

        n = 1500
        base = date_cls(2000, 1, 1)
        fake_client.get_json.return_value = [
            # Experiment k (visit k by date order) currently holds
            # experiment k+1's target, forming one n-long vacating chain
            # whose far end (visit 1's target) is free.
            _row(f"E{k}", f"SUB01_{k + 1:02d}_SE01_MR", date=str(base + timedelta(days=k)))
            for k in range(1, n + 1)
        ]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["skipped"] == []
        assert len(plan["renames"]) == n
        assert plan["renames"][0]["id"] == "E1"  # free end of the chain goes first

    def test_a_null_id_row_is_skipped_not_planned(self, fake_client) -> None:
        """A JSON null ID must not become the literal string "None" in a plan."""
        row = _row("ignored", "OLD1")
        row["ID"] = None
        fake_client.get_json.return_value = [row]
        service = SessionLabelService(fake_client)

        plan = service.plan_label_normalization("PROJ")

        assert plan["renames"] == []
        assert plan["skipped"] == [{"id": "", "label": "OLD1", "reason": "missing experiment ID"}]

    def test_row_missing_experiment_id_is_skipped_not_planned(self, fake_client) -> None:
        """An un-addressable row must not survive into a plan dry-run shows as valid."""
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
        """A 200 whose body is ["plugin disabled"] is malformed, not zero results."""
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
