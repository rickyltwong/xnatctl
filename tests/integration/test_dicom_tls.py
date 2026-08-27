"""Live TLS verification for DICOM C-STORE against a real TLS peer.

``tests/test_dicom_cstore_tls.py`` proves the TLS *plumbing* -- a context
gets built with verification on, ``tls_args`` reaches ``ae.associate`` --
entirely with mocks. It never actually completes a TLS handshake against a
real DICOM peer, so nothing had ever proven the client's TLS wiring
(``services/upload/dicom_store.py``) actually works against one.

This module closes that gap. It drives the real association code -- ``_c_echo``,
the same pre-flight check ``upload_dicom_store`` runs before sending any
files -- against ``docker/integration/tls-scp``, a minimal TLS-only
pynetdicom SCP, and checks that TLS actually negotiates: succeeding with the
right CA, and failing cleanly (not silently falling back to plaintext, not
raising) without ``--tls`` or with the wrong CA.

Start the fixture and run this file::

    docker compose -f docker-compose.integration.yml up -d --wait dicom-tls-scp
    uv run pytest tests/integration/test_dicom_tls.py -m integration

This fixture is independent of the XNAT service in the same compose file --
no XNAT session is needed here, only the TLS SCP. By default, an unreachable
SCP SKIPS the module (with the reason printed) rather than failing it, the
same as the rest of this tier when no server is up. Set
``XNATCTL_TEST_TLS_SCP_REQUIRED=1`` when the SCP is *supposed* to already be
running -- e.g. in a CI job that just brought the compose service up -- and
an unreachable SCP becomes a hard FAILURE instead. Without that, a container
that failed to start (crashed, image build broke, port already taken by
something else) reads as "nothing to test here" rather than as the red build
it actually is::

    docker compose -f docker-compose.integration.yml up -d --wait dicom-tls-scp
    XNATCTL_TEST_TLS_SCP_REQUIRED=1 \\
        uv run pytest tests/integration/test_dicom_tls.py -m integration
"""

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

#: Bind-mounted by docker-compose.integration.yml's dicom-tls-scp service;
#: entrypoint.sh (re)generates it on every container start.
CERT_DIR = (
    Path(__file__).resolve().parent.parent.parent / "docker" / "integration" / "tls-scp" / "certs"
)
CA_CERT = CERT_DIR / "ca.pem"

CONNECT_TIMEOUT_S = 30

#: Set to a truthy value to turn "SCP unreachable" from a skip into a
#: failure -- see the module docstring.
REQUIRED_ENV_VAR = "XNATCTL_TEST_TLS_SCP_REQUIRED"


def _wait_for_port(host: str, port: int, deadline: float) -> bool:
    """Poll a real TCP connect rather than trusting the healthcheck alone.

    Belt and suspenders with the compose healthcheck (which now also proves
    a real bind, not just that the cert file exists -- see
    docker-compose.integration.yml): a container can report healthy and
    still not yet be accepting connections in the instant `--wait` returns.
    Same reasoning as conftest.py's XNAT readiness fixture, which polls a
    real login rather than trusting a single endpoint.
    """
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
    """Skip by default; fail instead when the SCP was declared required.

    A silent skip is fine for a laptop run where nobody brought the fixture
    up. It is the wrong outcome in CI once something has asserted "this
    container is supposed to be running": a skip there is a green build that
    verified nothing -- exactly what this guard exists to prevent (a
    healthcheck can pass before the listener is actually bound).
    """
    if _is_required():
        pytest.fail(f"{message} ({REQUIRED_ENV_VAR} is set, so this is a failure, not a skip.)")
    pytest.skip(message)


@pytest.fixture(scope="module")
def tls_scp() -> Path:
    """Confirm the TLS SCP fixture is reachable; return its CA cert path.

    Skips (or fails -- see ``_skip_or_fail``) the whole module, with an
    actionable message, if the fixture was never started or is still
    starting up.
    """
    if not _wait_for_port(HOST, PORT, time.time() + CONNECT_TIMEOUT_S):
        _skip_or_fail(
            f"no TLS DICOM SCP answering at {HOST}:{PORT}. Start one with "
            "'docker compose -f docker-compose.integration.yml up -d --wait "
            "dicom-tls-scp'."
        )
    if not CA_CERT.exists():
        # Should not happen in practice given entrypoint.sh's ordering (the
        # cert is written before the SCP starts listening), but if it ever
        # does, it means something is actually broken, not merely "not up".
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
    """The SCP only accepts TLS; a plaintext association must not succeed.

    ``tls_scp`` is depended on only for the readiness/skip check -- the SCP
    being TLS-only is exactly what makes a plaintext attempt here meaningful.
    """
    assert _c_echo(HOST, PORT, CALLING_AET, CALLED_AET, tls_context=None) is False


def test_c_echo_fails_cleanly_with_the_wrong_ca(tls_scp: Path, tmp_path: Path) -> None:
    """A CA that never signed the server's cert must not verify it anyway."""
    crypto = pytest.importorskip("cryptography")
    assert crypto  # importorskip is the point; silences the unused-import lint
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
        # Marked as a CA so ssl accepts it as a trust bundle at all.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    wrong_ca = tmp_path / "wrong-ca.pem"
    wrong_ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    context = build_dicom_tls_context(ca_bundle=str(wrong_ca))

    assert _c_echo(HOST, PORT, CALLING_AET, CALLED_AET, tls_context=context) is False
