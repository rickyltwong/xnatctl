from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from xnatctl.services.upload.dicom_store import _c_echo, build_dicom_tls_context

pytestmark = pytest.mark.integration

HOST = "127.0.0.1"
PORT = 11112
CALLING_AET = "XNATCTL"
CALLED_AET = "TLSSCP"

CERT_DIR = (
    Path(__file__).resolve().parent.parent.parent / "docker" / "integration" / "tls-scp" / "certs"
)
CA_CERT = CERT_DIR / "ca.pem"

CONNECT_TIMEOUT_S = 30

REQUIRED_ENV_VAR = "XNATCTL_TEST_TLS_SCP_REQUIRED"


def _wait_for_port(host: str, port: int, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def _is_required() -> bool:
    return os.environ.get(REQUIRED_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}


def _skip_or_fail(message: str) -> None:
    if _is_required():
        pytest.fail(f"{message} ({REQUIRED_ENV_VAR} is set, so this is a failure, not a skip.)")
    pytest.skip(message)


@pytest.fixture(scope="module")
def tls_scp() -> Path:
    if not _wait_for_port(HOST, PORT, time.time() + CONNECT_TIMEOUT_S):
        _skip_or_fail(
            f"no TLS DICOM SCP answering at {HOST}:{PORT}. Start one with "
            "'docker compose -f docker-compose.integration.yml up -d --wait "
            "dicom-tls-scp'."
        )
    if not CA_CERT.exists():
        _skip_or_fail(
            f"TLS SCP is listening but its generated CA cert is not at {CA_CERT} "
            "-- something is wrong with the dicom-tls-scp container; check its "
            "logs rather than assuming this is a timing issue."
        )
    return CA_CERT


def test_c_echo_succeeds_over_tls_with_the_right_ca(tls_scp: Path) -> None:
    context = build_dicom_tls_context(ca_bundle=str(tls_scp))

    assert _c_echo(HOST, PORT, CALLING_AET, CALLED_AET, tls_context=context) is True


def test_c_echo_fails_cleanly_without_tls(tls_scp: Path) -> None:
    assert _c_echo(HOST, PORT, CALLING_AET, CALLED_AET, tls_context=None) is False


def test_c_echo_fails_cleanly_with_the_wrong_ca(tls_scp: Path, tmp_path: Path) -> None:
    crypto = pytest.importorskip("cryptography")
    assert crypto
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "not-the-scp")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    wrong_ca = tmp_path / "wrong-ca.pem"
    wrong_ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    context = build_dicom_tls_context(ca_bundle=str(wrong_ca))

    assert _c_echo(HOST, PORT, CALLING_AET, CALLED_AET, tls_context=context) is False
