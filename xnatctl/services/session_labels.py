"""Experiment label normalization service.

Recomputes each experiment's label in a project to the standardized
``{SUBJECT_LABEL}_{VISIT:02d}_SE{SESSION:02d}_{MODALITY}`` convention and
either previews or applies the renames. Backs ``xnatctl session
normalize-labels``.

Ported from the standalone ``scripts/apply_label_fixes.py`` maintenance
script, which combined this with a subject-rename pass. That pass is not
ported: it duplicated ``xnatctl subject rename`` (patterns-file/mapping/
regex renames, including merge-on-collision), which already covers it. The
experiment-label computation here -- grouping by subject and modality,
ordering by session date/time, and deriving the modality code from
``xsiType`` -- was the script's only capability with no CLI equivalent.

One behavioral change from the script: it only computed a subject's
experiment labels once that subject's *effective* (post-rename) label
already carried the project's ``{project}_`` prefix, because it ran the
subject-rename pass and this pass together and needed to skip subjects the
first pass had not gotten to yet. Run standalone, that gate would silently
skip every subject in a project that does not happen to use a
``{project}_`` naming convention, which is not a general property of XNAT
subject labels. It is dropped here; run ``subject rename`` first if a
project's subjects still need normalizing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any

from xnatctl.core.exceptions import InvalidIdentifierError, SessionExpiredError
from xnatctl.core.validation import quote_path_segment, validate_xnat_label

from .base import BaseService
from .hierarchy import HierarchyService

#: XSI type -> modality code used to build experiment labels.
XSI_MODALITY_MAP: dict[str, str] = {
    "xnat:mrsessiondata": "MR",
    "xnat:petsessiondata": "PET",
    "xnat:ctsessiondata": "CT",
    "xnat:crsessiondata": "CR",
    "xnat:dxsessiondata": "DX",
    "xnat:dx3dsessiondata": "DX3D",
    "xnat:mgsessiondata": "MG",
    "xnat:nmsessiondata": "NM",
    "xnat:ussessiondata": "US",
    "xnat:megsessiondata": "MEG",
    "xnat:eegsessiondata": "EEG",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
)

#: Columns fetched for the label computation. ``time``/``insert_date``/
#: ``insert_time`` are not part of ``SessionService.list_sessions``'s column
#: set -- that screen only needs ``date`` -- but same-day ordering here needs
#: all three.
_EXPERIMENT_COLUMNS = "ID,label,subject_label,xsiType,date,time,insert_date,insert_time"


def _parse_datetime(value: str) -> datetime | None:
    """Best-effort datetime parse across the formats XNAT emits, or None."""
    if not value:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    """Best-effort date parse, falling back to a full datetime parse."""
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    dt = _parse_datetime(value)
    return dt.date() if dt else None


def _parse_time(value: str) -> time | None:
    """Best-effort time-of-day parse, falling back to a full datetime parse."""
    if not value:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    dt = _parse_datetime(value)
    return dt.time() if dt else None


def _modality_from_xsi(xsi_type: str) -> str | None:
    """Map an experiment's ``xsiType`` to its modality code, or None."""
    return XSI_MODALITY_MAP.get((xsi_type or "").strip().lower())


def build_experiment_label(
    subject_label: str, visit_index: int, session_index: int, modality: str
) -> str:
    """Build the standardized target label for one experiment."""
    return f"{subject_label}_{visit_index:02d}_SE{session_index:02d}_{modality}"


class SessionLabelService(BaseService):
    """Compute and apply standardized experiment-label renames for a project."""

    def plan_label_normalization(
        self,
        project: str,
        *,
        subjects: Sequence[str] | None = None,
        subject_pattern: str | None = None,
        modalities: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Compute the experiment-label rename plan for a project.

        Pure read: issues no writes. ``apply_label_normalization`` executes
        exactly the plan this returns, so there is a single code path
        computing renames for both a dry run and a real one -- they cannot
        diverge because dry-run IS this call, with the write step skipped.

        Per subject, experiments are grouped by modality, then ordered by
        session date (assigning ``VISIT``) and, within a date, by
        time-of-day -- falling back to ``insert_date``/``insert_time`` when
        the session has no time of its own -- to break ties and assign
        ``SESSION``. Two same-day experiments in one modality with no
        time-of-day information at all cannot be ordered and are both
        skipped rather than assigned an arbitrary order.

        Args:
            project: Project ID.
            subjects: Optional subject-label allowlist. Default: every
                subject in the project.
            subject_pattern: Optional regex a subject label must match
                (checked in addition to *subjects*, not instead of it).
            modalities: Optional modality-code allowlist (e.g. ``["MR"]``).
                Default: every modality in :data:`XSI_MODALITY_MAP`.

        Returns:
            Dict with:

            - ``"renames"``: list of ``{"id", "subject", "old_label",
              "new_label"}`` rows to apply -- already filtered of no-ops,
              existing-label collisions, and same-target collisions.
            - ``"skipped"``: list of ``{"id", "label", "reason"}`` rows
              explaining why an experiment was left alone.
        """
        modality_filter = (
            {m.upper() for m in modalities} if modalities else set(XSI_MODALITY_MAP.values())
        )
        subject_re = re.compile(subject_pattern) if subject_pattern else None
        wanted = set(subjects) if subjects else None

        path = HierarchyService.build_experiment_collection_path(project)
        rows = HierarchyService.extract_rows_strict(
            self.client.get_json(path, params={"columns": _EXPERIMENT_COLUMNS}),
            f"listing experiments for project {project!r}",
        )

        by_subject: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            subject_label = row.get("subject_label")
            if not isinstance(subject_label, str) or not subject_label:
                continue
            if wanted and subject_label not in wanted:
                continue
            if subject_re and not subject_re.search(subject_label):
                continue
            by_subject.setdefault(subject_label, []).append(row)

        renames: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for subject_label in sorted(by_subject):
            self._plan_subject(
                subject_label,
                by_subject[subject_label],
                modality_filter=modality_filter,
                renames=renames,
                skipped=skipped,
            )

        return {"renames": renames, "skipped": skipped}

    def _plan_subject(
        self,
        subject_label: str,
        experiments: list[dict[str, Any]],
        *,
        modality_filter: set[str],
        renames: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Append this subject's renames/skips onto the running plan lists."""
        existing_labels = {e.get("label", "") for e in experiments if e.get("label")}

        by_modality_date: dict[str, dict[date, list[dict[str, Any]]]] = {}
        for exp in experiments:
            exp_id = str(exp.get("ID", ""))
            exp_label = str(exp.get("label", ""))
            modality = _modality_from_xsi(exp.get("xsiType", ""))

            if not modality:
                skipped.append(
                    {"id": exp_id, "label": exp_label, "reason": "unknown modality from xsiType"}
                )
                continue
            if modality not in modality_filter:
                skipped.append(
                    {
                        "id": exp_id,
                        "label": exp_label,
                        "reason": f"modality {modality} not in filter",
                    }
                )
                continue

            session_date = _parse_date(str(exp.get("date", "")))
            if not session_date:
                skipped.append({"id": exp_id, "label": exp_label, "reason": "missing session date"})
                continue

            session_time = _parse_time(str(exp.get("time", "")))
            insert_dt = _parse_datetime(str(exp.get("insert_date", "")))
            if not insert_dt:
                insert_date = _parse_date(str(exp.get("insert_date", "")))
                insert_time = _parse_time(str(exp.get("insert_time", "")))
                if insert_date and insert_time:
                    insert_dt = datetime.combine(insert_date, insert_time)

            order_time = session_time or (insert_dt.time() if insert_dt else None)
            by_modality_date.setdefault(modality, {}).setdefault(session_date, []).append(
                {
                    "id": exp_id,
                    "label": exp_label,
                    "modality": modality,
                    "order_time": order_time,
                    "insert_dt": insert_dt,
                }
            )

        seen_targets: dict[str, str] = {}
        for modality in sorted(by_modality_date):
            by_date = by_modality_date[modality]
            for visit_idx, session_date in enumerate(sorted(by_date), start=1):
                self._plan_visit(
                    subject_label,
                    by_date[session_date],
                    visit_idx=visit_idx,
                    existing_labels=existing_labels,
                    seen_targets=seen_targets,
                    renames=renames,
                    skipped=skipped,
                )

    def _plan_visit(
        self,
        subject_label: str,
        group: list[dict[str, Any]],
        *,
        visit_idx: int,
        existing_labels: set[str],
        seen_targets: dict[str, str],
        renames: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Assign SESSION order within one (modality, date) group and plan renames."""
        if len(group) > 1 and any(g["order_time"] is None for g in group):
            for g in group:
                skipped.append(
                    {
                        "id": g["id"],
                        "label": g["label"],
                        "reason": "missing time for same-day experiments; cannot assign SE order",
                    }
                )
            return

        group_sorted = sorted(
            group,
            key=lambda g: (
                g["order_time"] or time.min,
                g["insert_dt"] or datetime.min,
                g["label"],
                g["id"],
            ),
        )

        for session_idx, g in enumerate(group_sorted, start=1):
            target = build_experiment_label(subject_label, visit_idx, session_idx, g["modality"])
            if target == g["label"]:
                continue

            try:
                validate_xnat_label(target, "experiment label")
            except InvalidIdentifierError as e:
                skipped.append(
                    {"id": g["id"], "label": g["label"], "reason": f"invalid target label: {e}"}
                )
                continue

            if target in existing_labels and target != g["label"]:
                skipped.append(
                    {"id": g["id"], "label": g["label"], "reason": f"target label exists: {target}"}
                )
                continue

            # Defensive: given the enumeration above (visit_idx unique per
            # modality/date, session_idx unique per position within a
            # date's group), two experiments computing the same target
            # should not be reachable in practice. Kept as a hard refusal
            # rather than removed, in case a future change to the grouping
            # logic reopens the path -- silently applying one of a
            # colliding pair would be worse than a defensive check that
            # never fires.
            prior = seen_targets.get(target)
            if prior and prior != g["id"]:
                skipped.append(
                    {
                        "id": g["id"],
                        "label": g["label"],
                        "reason": f"target label conflict: {target}",
                    }
                )
                continue

            seen_targets[target] = g["id"]
            renames.append(
                {
                    "id": g["id"],
                    "subject": subject_label,
                    "old_label": g["label"],
                    "new_label": target,
                }
            )

    def apply_label_normalization(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Execute a rename plan produced by :meth:`plan_label_normalization`.

        Renames are applied one experiment at a time; a failure on one does
        not stop the rest -- it is collected in ``"failed"`` instead, the
        same per-item isolation the ported script used. A session expiry
        aborts the whole run rather than being counted as a per-item
        failure, since XNAT would otherwise reject every remaining rename
        in a way indistinguishable from a real per-experiment error.

        Args:
            plan: The dict returned by :meth:`plan_label_normalization`.
                Only its ``"renames"`` key is used.

        Returns:
            Dict with ``"renamed"`` (count successfully applied) and
            ``"failed"`` (list of ``{"id", "old_label", "new_label",
            "error"}`` rows for renames that raised).
        """
        renamed = 0
        failed: list[dict[str, Any]] = []
        for item in plan["renames"]:
            try:
                self.client.put(
                    f"/data/experiments/{quote_path_segment(item['id'])}",
                    params={"label": item["new_label"]},
                )
                renamed += 1
            except SessionExpiredError:
                raise
            except Exception as exc:  # noqa: BLE001  # per-experiment isolation across a rename batch
                failed.append(
                    {
                        "id": item["id"],
                        "old_label": item["old_label"],
                        "new_label": item["new_label"],
                        "error": str(exc),
                    }
                )
        return {"renamed": renamed, "failed": failed}
