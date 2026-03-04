"""Tests for transfer-specific exceptions."""

from __future__ import annotations

from xnatctl.core.exceptions import (
    TransferCircuitBreakerError,
    TransferConfigError,
    TransferConflictError,
    TransferError,
    TransferVerificationError,
)


class TestTransferExceptions:
    def test_transfer_error_is_operation_error(self) -> None:
        err = TransferError("something broke")
        assert str(err) == "something broke (operation=transfer)"
        assert err.details.get("operation") == "transfer"

    def test_conflict_error_has_entity_info(self) -> None:
        err = TransferConflictError(
            entity_type="subject",
            local_id="XNAT_S001",
            remote_id="XNAT_S999",
            reason="label mismatch",
        )
        assert "subject" in str(err)
        assert "label mismatch" in str(err)
        assert err.entity_type == "subject"

    def test_circuit_breaker_error(self) -> None:
        err = TransferCircuitBreakerError(failures=5, max_failures=5)
        assert "5" in str(err)
        assert err.failures == 5

    def test_verification_error(self) -> None:
        err = TransferVerificationError(
            entity_id="XNAT_E001",
            expected=10,
            actual=8,
        )
        assert "XNAT_E001" in str(err)
        assert err.expected == 10
        assert err.actual == 8

    def test_config_error(self) -> None:
        err = TransferConfigError("bad filter", field="sync_type")
        assert "bad filter" in str(err)
