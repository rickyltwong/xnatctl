"""Audit-trail tests (SEC-07).

`AuditLogger` existed but was dead: the only references in the package were two
re-exports, and no command ever called `log_operation`. It also logged to
stderr rather than anywhere persistent, and did not redact its `details` dict.
Destructive commands therefore left no local trace at all.

The trail is written from `confirm_destructive`, so carrying that decorator is
what marks a command as auditable -- coverage is automatic rather than
per-command.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import make_authenticated_cli

from xnatctl.cli.main import cli
from xnatctl.core.logging import AUDIT_LOG_MAX_BYTES, AuditLogger


def records(path: Path) -> list[dict]:
    """Parse the JSON-lines audit log."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def audit_log(isolate_audit_log: Path) -> Path:
    """The redirected audit-log path (see the autouse fixture in conftest)."""
    return isolate_audit_log


# =============================================================================
# AuditLogger itself
# =============================================================================


def test_record_is_one_json_line_with_the_expected_fields(audit_log: Path) -> None:
    AuditLogger().log_operation(
        "subject.delete",
        project="PROJ",
        subject="SUB001",
        user="admin",
        server="https://xnat.example.org",
        command="xnatctl subject delete",
    )

    (entry,) = records(audit_log)
    assert entry["operation"] == "subject.delete"
    assert entry["project"] == "PROJ"
    assert entry["subject"] == "SUB001"
    assert entry["user"] == "admin"
    assert entry["command"] == "xnatctl subject delete"
    assert entry["success"] is True
    assert "timestamp" in entry


def test_records_append_rather_than_overwrite(audit_log: Path) -> None:
    logger = AuditLogger()
    logger.log_operation("first")
    logger.log_operation("second")

    assert [e["operation"] for e in records(audit_log)] == ["first", "second"]


def test_log_file_is_owner_only(audit_log: Path) -> None:
    """It records which subjects were deleted from which server; same class of
    secret as the session cache."""
    AuditLogger().log_operation("subject.delete", subject="SUB001")

    assert stat.S_IMODE(audit_log.stat().st_mode) == 0o600


def test_existing_world_readable_log_is_tightened(audit_log: Path) -> None:
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.write_text("")
    os.chmod(audit_log, 0o644)

    AuditLogger().log_operation("subject.delete")

    assert stat.S_IMODE(audit_log.stat().st_mode) == 0o600


def test_failure_is_recorded_with_the_error_class(audit_log: Path) -> None:
    AuditLogger().log_operation("subject.delete", success=False, error="PermissionDeniedError")

    (entry,) = records(audit_log)
    assert entry["success"] is False
    assert entry["error"] == "PermissionDeniedError"


def test_dry_run_is_marked(audit_log: Path) -> None:
    AuditLogger().log_operation("subject.delete", dry_run=True)

    assert records(audit_log)[0]["dry_run"] is True


def test_real_run_does_not_carry_a_dry_run_key(audit_log: Path) -> None:
    AuditLogger().log_operation("subject.delete")

    assert "dry_run" not in records(audit_log)[0]


def test_url_credentials_are_redacted(audit_log: Path) -> None:
    AuditLogger().log_operation(
        "project.transfer",
        server="https://admin:s3cret@xnat.example.org",
        details={"dest": "https://x.example.org/a?token=abc"},
    )

    text = audit_log.read_text()
    assert "s3cret" not in text
    assert "abc" not in text
    assert "token=***" in text


def test_write_failure_warns_but_does_not_raise(
    audit_log: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bookkeeping must never be the reason a delete fails."""
    logger = AuditLogger(log_file=audit_log)

    with patch("xnatctl.core.logging.open_private_append", side_effect=OSError("read-only fs")):
        logger.log_operation("subject.delete")

    assert any("Could not write the audit log" in r.getMessage() for r in caplog.records)


def test_log_rotates_once_past_the_size_limit(audit_log: Path) -> None:
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.write_text("x" * (AUDIT_LOG_MAX_BYTES + 1))

    AuditLogger().log_operation("subject.delete")

    rotated = audit_log.with_name(audit_log.name + ".1")
    assert rotated.exists(), "the oversized log should have been rolled aside"
    assert len(records(audit_log)) == 1, "the live log restarts with just the new record"


def test_small_log_is_not_rotated(audit_log: Path) -> None:
    AuditLogger().log_operation("first")
    AuditLogger().log_operation("second")

    assert not audit_log.with_name(audit_log.name + ".1").exists()
    assert len(records(audit_log)) == 2


# =============================================================================
# Wiring: destructive commands are audited automatically
# =============================================================================


def test_destructive_command_writes_exactly_one_record(audit_log: Path) -> None:
    harness = make_authenticated_cli(default_project="PROJ")
    harness.client.delete.return_value = MagicMock(status_code=200)

    result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--yes"])

    assert result.exit_code == 0
    (entry,) = records(audit_log)
    assert entry["subject"] == "SUB001"
    assert entry["project"] == "PROJ"
    assert entry["success"] is True
    assert entry["details"]["confirmed"] is True


def test_record_names_the_profile_even_when_it_was_not_passed(audit_log: Path) -> None:
    """profile_name is None whenever the default profile was used -- which is
    the common case -- so the field falls back to the config's default."""
    harness = make_authenticated_cli(default_project="PROJ")
    harness.client.delete.return_value = MagicMock(status_code=200)

    harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--yes"])

    (entry,) = records(audit_log)
    assert entry["profile"] == harness.ctx.config.default_profile
    assert entry["server"] == harness.client.base_url


def test_dry_run_command_is_audited_as_a_preview(audit_log: Path) -> None:
    harness = make_authenticated_cli(default_project="PROJ")

    result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--dry-run"])

    assert result.exit_code == 0
    (entry,) = records(audit_log)
    assert entry["dry_run"] is True
    assert entry["success"] is True
    # A dry run never builds a client, so the server has to come from the
    # profile -- "which server was this aimed at" is the point of the record.
    assert entry["server"] == "https://xnat.example.org"


def test_failed_command_records_the_real_error_class(audit_log: Path) -> None:
    """handle_errors collapses failures to SystemExit; the record must still
    name what actually went wrong."""
    from xnatctl.core.exceptions import PermissionDeniedError

    harness = make_authenticated_cli(default_project="PROJ")
    harness.client.delete.side_effect = PermissionDeniedError(
        resource="SUB001", operation="delete", url="https://xnat.example.org"
    )

    result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--yes"])

    assert result.exit_code != 0
    (entry,) = records(audit_log)
    assert entry["success"] is False
    assert entry["error"] == "PermissionDeniedError"


def test_declined_confirmation_is_not_audited(audit_log: Path) -> None:
    """Nothing was attempted, so recording it would bury real changes."""
    harness = make_authenticated_cli(default_project="PROJ")

    result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ"], input="n\n")

    assert result.exit_code != 0
    assert records(audit_log) == []


def test_read_only_command_is_not_audited(audit_log: Path) -> None:
    harness = make_authenticated_cli(default_project="PROJ")
    harness.client.paginate.return_value = iter([])

    harness.invoke(["subject", "list", "-P", "PROJ"])

    assert records(audit_log) == []


def test_secret_flags_never_reach_the_record(audit_log: Path) -> None:
    """The denylist is keyed on parameter name, so a credential flag on a
    destructive command cannot be recorded even by accident."""
    from xnatctl.cli.common import _audit_details

    details = _audit_details(
        {
            "project": "PROJ",
            "password": "hunter2",
            "dest_pass": "hunter2",
            "token": "abc",
            "subject_id": "SUB001",
        }
    )

    assert details == {"project": "PROJ", "subject_id": "SUB001"}


def test_audit_failure_does_not_break_the_command(audit_log: Path) -> None:
    harness = make_authenticated_cli(default_project="PROJ")
    harness.client.delete.return_value = MagicMock(status_code=200)

    with patch("xnatctl.cli.common.get_audit_logger", side_effect=RuntimeError("boom")):
        result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--yes"])

    assert result.exit_code == 0, "an audit failure must not fail the delete"


def test_runner_is_importable() -> None:
    """Guard the harness import so a conftest rename fails loudly here."""
    assert isinstance(CliRunner(), CliRunner)
    assert cli is not None
