"""
apply_label_fixes.py

Automated subject + experiment label corrections across XNAT projects.

This is a wrapper-style maintenance script intended for scheduled execution
(cron / job runner). It is not part of the core `xnatctl` CLI surface.

It applies two kinds of fixes:
1) Subject renames/merges based on regex patterns (per project)
2) Experiment label normalization to a standardized convention:
   {SUBJECT_LABEL}_{VISIT:02d}_SE{SESSION:02d}_{MODALITY}

Usage (dry-run):
  uv run python scripts/apply_label_fixes.py scripts/example_patterns.json -v

Apply changes:
  uv run python scripts/apply_label_fixes.py scripts/example_patterns.json --execute -v

Credentials:
  - Cached session from `xnatctl auth login`, OR
  - Environment: XNAT_USER / XNAT_PASS
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from xnatctl.core.exceptions import SessionExpiredError

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

log = logging.getLogger(__name__)

# XSI type -> modality code mapping (used to build experiment labels)
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

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d")
TIME_FORMATS = ("%H:%M:%S", "%H:%M")
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
)


def load_patterns_config(config_path: Path) -> dict[str, object]:
    """Load patterns configuration from a JSON file."""
    with config_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return cast(dict[str, object], data)


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    dt = _parse_datetime(value)
    return dt.date() if dt else None


def _parse_time(value: str) -> time | None:
    if not value:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    dt = _parse_datetime(value)
    return dt.time() if dt else None


def _modality_from_xsi(xsi_type: str) -> str | None:
    key = (xsi_type or "").strip().lower()
    return XSI_MODALITY_MAP.get(key)


def _build_target_label(
    subject_label: str, visit_index: int, session_index: int, modality: str
) -> str:
    return f"{subject_label}_{visit_index:02d}_SE{session_index:02d}_{modality}"


# =============================================================================
# XNAT API helpers (using xnatctl's XNATClient)
# =============================================================================


def _list_subjects(client: XNATClient, project: str) -> list[dict[str, Any]]:
    path = f"/data/projects/{project}/subjects"
    resp = client.get(path, params={"format": "json"})
    data = resp.json()
    return data.get("ResultSet", {}).get("Result", [])


def _list_subject_experiments(
    client: XNATClient,
    project: str,
    subject: str,
) -> list[dict[str, Any]]:
    path = f"/data/projects/{project}/subjects/{subject}/experiments"
    params = {
        "format": "json",
        "columns": "ID,label,xsiType,date,time,insert_date,insert_time",
    }
    resp = client.get(path, params=params)
    data = resp.json()
    return data.get("ResultSet", {}).get("Result", [])


def _rename_subject(client: XNATClient, project: str, old_label: str, new_label: str) -> None:
    path = f"/data/projects/{project}/subjects/{old_label}"
    client.put(path, params={"label": new_label})


def _merge_subject(client: XNATClient, project: str, source: str, target: str) -> None:
    experiments = _list_subject_experiments(client, project, source)
    for exp in experiments:
        exp_id = exp.get("ID")
        if exp_id:
            client.put(
                f"/data/experiments/{exp_id}", params={"xnat:experimentData/subject_ID": target}
            )
    client.delete(f"/data/projects/{project}/subjects/{source}")


def _rename_experiment(client: XNATClient, exp_id: str, new_label: str) -> None:
    client.put(f"/data/experiments/{exp_id}", params={"label": new_label})


# =============================================================================
# Subject patterns
# =============================================================================


def apply_subject_patterns(
    client: XNATClient,
    project: str,
    patterns: list[dict[str, Any]],
    *,
    execute: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    if not patterns:
        log.info("No subject rename patterns for project %s", project)
        return {
            "renamed": 0,
            "merged": 0,
            "skipped": 0,
            "errors": 0,
            "renamed_map": {},
            "merged_map": {},
        }

    dry_run = not execute
    mode = "DRY-RUN" if dry_run else "EXECUTE"

    log.info("=" * 60)
    log.info("Subject rename: %s", mode)
    log.info("Project: %s", project)
    log.info("Patterns: %s", len(patterns))
    log.info("=" * 60)

    total_renamed = 0
    total_merged = 0
    total_skipped = 0
    total_errors = 0
    renamed_map: dict[str, str] = {}
    merged_map: dict[str, str] = {}

    subjects_data = _list_subjects(client, project)
    subject_labels = {s.get("label", s.get("ID", "")): s for s in subjects_data}

    for pattern in patterns:
        match = pattern.get("match")
        to = pattern.get("to")
        desc = pattern.get("description", "")

        if not match or not to or not isinstance(match, str) or not isinstance(to, str):
            log.warning("Invalid pattern entry (missing match/to); skipping.")
            total_errors += 1
            continue

        log.info("-" * 60)
        log.info("Pattern: %s", match)
        log.info("To:      %s", to)
        if desc:
            log.info("         (%s)", desc)

        try:
            regex = re.compile(match)
        except re.error as e:
            log.error("Invalid regex: %s - %s", match, e)
            total_errors += 1
            continue

        renamed: dict[str, str] = {}
        merged: dict[str, str] = {}
        skipped: list[tuple[str, str]] = []

        for label in list(subject_labels):
            m = regex.match(label)
            if not m:
                continue

            target = to.replace("{project}", project)
            for i, group in enumerate(m.groups(), 1):
                target = target.replace(f"{{{i}}}", group or "")

            if target == label:
                skipped.append((label, "no change"))
                continue

            if target in subject_labels:
                if execute:
                    try:
                        _merge_subject(client, project, label, target)
                        merged[label] = target
                        # Keep local view in sync for subsequent checks.
                        subject_labels.pop(label, None)
                    except SessionExpiredError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        log.error("Failed to merge %s -> %s: %s", label, target, e)
                        total_errors += 1
                else:
                    merged[label] = target
            else:
                if execute:
                    try:
                        _rename_subject(client, project, label, target)
                        renamed[label] = target
                        # Keep local view in sync for subsequent checks.
                        subject_labels[target] = subject_labels.pop(label, {})
                    except SessionExpiredError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        log.error("Failed to rename %s -> %s: %s", label, target, e)
                        total_errors += 1
                else:
                    renamed[label] = target

        if renamed:
            log.info("Renamed (%s):", len(renamed))
            for old, new in renamed.items():
                log.info("  %s -> %s", old, new)

        if merged:
            log.info("Merged (%s):", len(merged))
            for old, new in merged.items():
                log.info("  %s -> %s", old, new)

        if skipped and verbose:
            log.info("Skipped (%s):", len(skipped))
            for label, reason in skipped:
                log.info("  %s: %s", label, reason)

        total_renamed += len(renamed)
        total_merged += len(merged)
        total_skipped += len(skipped)
        renamed_map.update(renamed)
        merged_map.update(merged)

    log.info("=" * 60)
    log.info(
        "Subject summary: %s renamed, %s merged, %s skipped",
        total_renamed,
        total_merged,
        total_skipped,
    )
    if dry_run:
        log.info("This was a DRY-RUN. Use --execute to apply changes.")
    log.info("=" * 60)

    return {
        "renamed": total_renamed,
        "merged": total_merged,
        "skipped": total_skipped,
        "errors": total_errors,
        "renamed_map": renamed_map,
        "merged_map": merged_map,
    }


# =============================================================================
# Experiment label fixes
# =============================================================================


def apply_experiment_label_fixes(
    client: XNATClient,
    project: str,
    *,
    subject_label_overrides: dict[str, str] | None = None,
    subjects: Sequence[str] | None = None,
    subject_pattern: str | None = None,
    modalities: Sequence[str] | None = None,
    execute: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    if modalities is None:
        modalities = sorted(set(XSI_MODALITY_MAP.values()))
    modalities_set = {m.upper() for m in modalities}

    dry_run = not execute
    mode = "DRY-RUN" if dry_run else "EXECUTE"

    log.info("=" * 60)
    log.info("Experiment label fixes: %s", mode)
    log.info("Project: %s", project)
    log.info("Modalities: %s", ", ".join(sorted(modalities_set)))
    if subjects:
        log.info("Subject filter: %s", ", ".join(subjects))
    if subject_pattern:
        log.info("Subject pattern: %s", subject_pattern)
    log.info("=" * 60)

    subject_re = re.compile(subject_pattern) if subject_pattern else None
    overrides = subject_label_overrides or {}
    subjects_data = _list_subjects(client, project)
    wanted = set(subjects) if subjects else None

    # Group actual XNAT subjects by their *effective* (post-fix) label.
    # This lets experiment labels be computed using the normalized subject label,
    # even during dry-run when the subject rename hasn't been applied yet.
    prefix = f"{project}_"
    subject_groups: dict[str, list[str]] = {}

    total_renamed = 0
    total_skipped = 0
    skipped_subjects = 0
    total_failed = 0

    for subj in subjects_data:
        actual = subj.get("label", subj.get("ID", ""))
        if not isinstance(actual, str) or not actual:
            continue

        effective = overrides.get(actual, actual)

        # Apply filters to either the actual or effective label.
        if wanted and actual not in wanted and effective not in wanted:
            continue
        if subject_re and not (subject_re.search(actual) or subject_re.search(effective)):
            continue

        if not isinstance(effective, str) or not effective.startswith(prefix):
            if verbose:
                log.info("Skipping subject %s: not normalized to project prefix", actual)
            skipped_subjects += 1
            continue

        subject_groups.setdefault(effective, []).append(actual)

    for effective_label, actual_labels in sorted(subject_groups.items()):
        # Gather experiments across all subjects that will become this label (merge-safe planning).
        experiments: list[dict[str, Any]] = []
        for actual in actual_labels:
            experiments.extend(_list_subject_experiments(client, project, actual))

        if not experiments:
            continue

        existing_labels = {e.get("label", "") for e in experiments if e.get("label")}
        by_modality_date: dict[str, dict[date, list[dict[str, Any]]]] = {}
        skipped: list[tuple[str, str, str]] = []

        for exp in experiments:
            exp_id = exp.get("ID", "")
            exp_label = exp.get("label", "")
            modality = _modality_from_xsi(exp.get("xsiType", ""))

            if not modality:
                skipped.append((str(exp_id), str(exp_label), "unknown modality from xsiType"))
                continue

            if modality not in modalities_set:
                skipped.append((str(exp_id), str(exp_label), f"modality {modality} not in filter"))
                continue

            session_date = _parse_date(str(exp.get("date", "")))
            if not session_date:
                skipped.append((str(exp_id), str(exp_label), "missing session date"))
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
                    "ID": str(exp_id),
                    "label": str(exp_label),
                    "modality": modality,
                    "order_time": order_time,
                    "insert_dt": insert_dt,
                }
            )

        rename_plan: list[tuple[str, str, str]] = []
        seen_targets: dict[str, str] = {}

        for modality in sorted(by_modality_date.keys()):
            by_date = by_modality_date[modality]

            for visit_idx, session_date in enumerate(sorted(by_date.keys()), start=1):
                group = by_date[session_date]

                if len(group) > 1 and any(g["order_time"] is None for g in group):
                    for g in group:
                        skipped.append(
                            (
                                cast(str, g["ID"]),
                                cast(str, g["label"]),
                                "missing time for same-day experiments; cannot assign SE order",
                            )
                        )
                    continue

                group_sorted = sorted(
                    group,
                    key=lambda g: (
                        g["order_time"] or time.min,
                        g["insert_dt"] or datetime.min,
                        g["label"],
                        g["ID"],
                    ),
                )

                for session_idx, g in enumerate(group_sorted, start=1):
                    target = _build_target_label(
                        effective_label, visit_idx, session_idx, cast(str, g["modality"])
                    )
                    if target == g["label"]:
                        continue

                    if target in existing_labels and target != g["label"]:
                        skipped.append((g["ID"], g["label"], f"target label exists: {target}"))
                        continue

                    prior = seen_targets.get(target)
                    if prior and prior != g["ID"]:
                        skipped.append((g["ID"], g["label"], f"target label conflict: {target}"))
                        continue

                    seen_targets[target] = g["ID"]
                    rename_plan.append((g["ID"], g["label"], target))

        if rename_plan:
            src_suffix = (
                f" (from {', '.join(sorted(actual_labels))})" if len(actual_labels) > 1 else ""
            )
            log.info("Subject: %s%s", effective_label, src_suffix)
            log.info("Renames (%s):", len(rename_plan))
            for exp_id, old_label, new_label in rename_plan:
                log.info("  %s %s -> %s", exp_id, old_label, new_label)

        if skipped and verbose:
            log.info("Skipped (%s):", len(skipped))
            for exp_id, old_label, reason in skipped:
                log.info("  %s %s: %s", exp_id, old_label, reason)

        if execute:
            for exp_id, old_label, new_label in rename_plan:
                try:
                    _rename_experiment(client, exp_id, new_label)
                    total_renamed += 1
                except SessionExpiredError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    total_failed += 1
                    log.error(
                        "Failed to rename %s (%s) -> %s: %s", exp_id, old_label, new_label, exc
                    )
        else:
            total_renamed += len(rename_plan)

        total_skipped += len(skipped)

    log.info("=" * 60)
    log.info("Experiment summary: %s planned/renamed, %s skipped", total_renamed, total_skipped)
    if skipped_subjects:
        log.info("Skipped subjects (not normalized): %s", skipped_subjects)
    if execute and total_failed:
        log.error("FAILED: %s renames", total_failed)
    if not execute:
        log.info("This was a DRY-RUN. Use --execute to apply changes.")
    log.info("=" * 60)

    return {
        "renamed": total_renamed,
        "skipped": total_skipped,
        "failed": total_failed,
        "skipped_subjects": skipped_subjects,
    }


def apply_label_fixes(
    client: XNATClient,
    config_path: Path,
    *,
    projects: Sequence[str] | None = None,
    subjects: Sequence[str] | None = None,
    subject_pattern: str | None = None,
    modalities: Sequence[str] | None = None,
    execute: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    config = load_patterns_config(config_path)
    patterns = cast(list[dict[str, Any]], config.get("patterns", []))

    if projects:
        project_ids = list(projects)
    else:
        project_ids = sorted({cast(str, p.get("project")) for p in patterns if p.get("project")})

    if not project_ids:
        log.error("No projects found in config. Provide --project explicitly.")
        return {"subject_results": {}, "experiment_results": {}, "failed": True}

    log.info("Processing %s project(s): %s", len(project_ids), ", ".join(project_ids))

    overall_failed = False
    subject_results: dict[str, Any] = {}
    experiment_results: dict[str, Any] = {}

    for project_id in project_ids:
        log.info("\n%s", "#" * 60)
        log.info("# PROJECT: %s", project_id)
        log.info("%s\n", "#" * 60)

        project_patterns = [p for p in patterns if p.get("project") == project_id]
        subj_result = apply_subject_patterns(
            client,
            project_id,
            project_patterns,
            execute=execute,
            verbose=verbose,
        )
        subject_results[project_id] = subj_result

        if subj_result["errors"]:
            log.error("Subject step had errors; skipping experiment step for this project.")
            overall_failed = True
            continue

        label_overrides = {
            **cast(dict[str, str], subj_result.get("renamed_map", {})),
            **cast(dict[str, str], subj_result.get("merged_map", {})),
        }

        exp_result = apply_experiment_label_fixes(
            client,
            project_id,
            subject_label_overrides=label_overrides,
            subjects=subjects,
            subject_pattern=subject_pattern,
            modalities=modalities,
            execute=execute,
            verbose=verbose,
        )
        experiment_results[project_id] = exp_result

        if exp_result["failed"]:
            overall_failed = True

    log.info("\n%s", "=" * 60)
    log.info("FINAL SUMMARY")
    log.info("%s", "=" * 60)

    for project_id in project_ids:
        subj = subject_results.get(project_id, {})
        exp = experiment_results.get(project_id, {})
        log.info(
            "%s: subjects(%s renamed, %s merged) | experiments(%s renamed)",
            project_id,
            subj.get("renamed", 0),
            subj.get("merged", 0),
            exp.get("renamed", 0),
        )

    if not execute:
        log.info("\nThis was a DRY-RUN. Use --execute to apply changes.")

    return {
        "subject_results": subject_results,
        "experiment_results": experiment_results,
        "failed": overall_failed,
    }


def main() -> None:
    import argparse

    # Ensure repo root import works when running as a script.
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from xnatctl.core.auth import AuthManager
    from xnatctl.core.client import XNATClient
    from xnatctl.core.config import Config, get_credentials
    from xnatctl.core.exceptions import AuthenticationError, SessionExpiredError

    parser = argparse.ArgumentParser(
        description="Apply subject + experiment label fixes (patterns + standardized experiment labels)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # Dry-run all projects in config
  uv run python scripts/apply_label_fixes.py scripts/example_patterns.json -v

  # Execute changes
  uv run python scripts/apply_label_fixes.py scripts/example_patterns.json --execute -v

  # Specific project only
  uv run python scripts/apply_label_fixes.py scripts/example_patterns.json --project TESTPROJ -v
        """,
    )
    parser.add_argument("config", type=Path, help="Path to patterns JSON config file")
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        help="Limit to specific projects (repeatable)",
    )
    parser.add_argument(
        "--subject",
        dest="subjects",
        action="append",
        help="Limit to specific subjects (repeatable)",
    )
    parser.add_argument("--subject-pattern", help="Regex to filter subject labels")
    parser.add_argument(
        "--modality",
        dest="modalities",
        action="append",
        help="Modalities to process (default: all known modalities)",
    )
    parser.add_argument("--execute", action="store_true", help="Execute changes (default: dry-run)")
    parser.add_argument("--profile", "-p", help="xnatctl profile to use")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    # httpx emits "HTTP Request: ..." at INFO. Only show those when -v/--verbose is enabled.
    if args.verbose:
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("httpcore").setLevel(logging.INFO)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not args.config.exists():
        parser.error(f"Config file not found: {args.config}")

    config = Config.load()
    auth_mgr = AuthManager()

    profile = config.get_profile(args.profile)
    session_token = auth_mgr.get_session_token(profile.url)
    username, password = get_credentials(profile)

    def login() -> XNATClient:
        """Authenticate using credentials and refresh cached session."""
        if not username or not password:
            raise AuthenticationError(profile.url, "No credentials available for re-authentication")

        c = XNATClient(
            base_url=profile.url,
            username=username,
            password=password,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
            auto_reauth=True,
        )
        c.authenticate()

        # Best-effort: cache refreshed session token for subsequent runs.
        if c.session_token:
            cached_user = username
            try:
                info = c.whoami()
                u = info.get("username")
                if isinstance(u, str) and u:
                    cached_user = u
            except Exception:
                pass
            auth_mgr.save_session(token=c.session_token, url=profile.url, username=cached_user)

        return c

    if session_token:
        client = XNATClient(
            base_url=profile.url,
            username=username,
            password=password,
            session_token=session_token,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
            auto_reauth=True,
        )
        # Validate cached token early; if rejected, re-authenticate and refresh cache.
        try:
            client.whoami()
        except AuthenticationError:
            client.close()
            client = login()
    else:
        if not username or not password:
            parser.error(
                "No credentials found. Set XNAT_USER/XNAT_PASS or run 'xnatctl auth login'."
            )
        client = login()

    try:
        retried = False
        while True:
            try:
                result = apply_label_fixes(
                    client,
                    args.config,
                    projects=args.projects,
                    subjects=args.subjects,
                    subject_pattern=args.subject_pattern,
                    modalities=args.modalities,
                    execute=args.execute,
                    verbose=args.verbose,
                )
                break
            except SessionExpiredError as e:
                if retried:
                    raise
                if not username or not password:
                    raise AuthenticationError(
                        profile.url,
                        f"{e}. Re-authentication requires credentials; run 'xnatctl auth login' or set XNAT_USER/XNAT_PASS.",
                    ) from e
                log.warning("Authentication failed (%s). Re-authenticating and retrying...", e)
                retried = True
                client.close()
                client = login()
        raise SystemExit(1 if result["failed"] else 0)
    finally:
        client.close()


if __name__ == "__main__":
    main()
