from __future__ import annotations

import ssl
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import UploadError
from xnatctl.services.upload.dicom_store import _tls_kwargs, build_dicom_tls_context


def _write_self_signed(tmp_path: Path) -> tuple[Path, Path]:
    crypto = pytest.importorskip("cryptography")
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    assert crypto
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "xnatctl-test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        # Marked as a CA so it is usable as a trust bundle: ssl only reports
        # certs through get_ca_certs() when they carry this constraint.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / "client.crt"
    key_path = tmp_path / "client.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TestContext:
    def test_verification_is_on(self) -> None:
        context = build_dicom_tls_context()

        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_there_is_no_way_to_turn_verification_off(self) -> None:
        import inspect

        params = inspect.signature(build_dicom_tls_context).parameters

        assert "verify" not in params
        assert "insecure" not in params
        assert not any("verify" in p or "insecure" in p for p in params)

    def test_a_ca_bundle_is_loaded(self, tmp_path: Path) -> None:
        cert_path, _key = _write_self_signed(tmp_path)

        context = build_dicom_tls_context(ca_bundle=str(cert_path))

        assert context.get_ca_certs(), "the bundle was not loaded"

    def test_a_client_cert_and_key_are_loaded(self, tmp_path: Path) -> None:
        cert_path, key_path = _write_self_signed(tmp_path)

        context = build_dicom_tls_context(client_cert=str(cert_path), client_key=str(key_path))

        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_a_missing_cert_file_raises_an_actionable_error(self, tmp_path: Path) -> None:
        with pytest.raises(UploadError) as excinfo:
            build_dicom_tls_context(client_cert=str(tmp_path / "nope.crt"))

        assert "client certificate" in str(excinfo.value)

    def test_a_mismatched_key_raises_rather_than_silently_continuing(self, tmp_path: Path) -> None:
        cert_path, _key = _write_self_signed(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        _cert2, key2 = _write_self_signed(other)

        with pytest.raises(UploadError):
            build_dicom_tls_context(client_cert=str(cert_path), client_key=str(key2))


class TestAssociationKwargs:
    def test_no_context_means_no_tls_args(self) -> None:
        assert _tls_kwargs(None, "scp.example.org") == {}

    def test_a_context_becomes_tls_args(self) -> None:
        context = build_dicom_tls_context()

        kwargs = _tls_kwargs(context, "scp.example.org")

        assert kwargs["tls_args"][0] is context

    def test_the_hostname_is_passed_for_verification(self) -> None:
        context = build_dicom_tls_context()

        kwargs = _tls_kwargs(context, "scp.example.org")

        assert kwargs["tls_args"][1] == "scp.example.org"


class TestServiceWiring:
    @staticmethod
    def _service():
        from xnatctl.core.client import XNATClient
        from xnatctl.services.upload import UploadService

        client = MagicMock(spec=XNATClient)
        client.base_url = "https://x"
        return UploadService(client)

    def test_tls_reaches_the_association(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.services.upload.dicom_store as uploads

        captured: dict[str, object] = {}

        def fake_echo(host, port, calling, called, tls_context=None):
            captured["tls_context"] = tls_context
            return False

        monkeypatch.setattr(uploads, "_c_echo", fake_echo)
        (tmp_path / "a.dcm").write_bytes(b"DICM")

        with pytest.raises(RuntimeError, match="C-ECHO failed"):
            self._service().upload_dicom_store(
                dicom_root=tmp_path, host="scp.example.org", called_aet="AET", tls=True
            )

        assert isinstance(captured["tls_context"], ssl.SSLContext)

    def test_plaintext_passes_no_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.services.upload.dicom_store as uploads

        captured: dict[str, object] = {"tls_context": "unset"}

        def fake_echo(host, port, calling, called, tls_context=None):
            captured["tls_context"] = tls_context
            return False

        monkeypatch.setattr(uploads, "_c_echo", fake_echo)
        (tmp_path / "a.dcm").write_bytes(b"DICM")

        with pytest.raises(RuntimeError, match="C-ECHO failed"):
            self._service().upload_dicom_store(
                dicom_root=tmp_path, host="scp.example.org", called_aet="AET"
            )

        assert captured["tls_context"] is None

    def test_plaintext_logs_the_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        import xnatctl.services.upload.dicom_store as uploads

        monkeypatch.setattr(uploads, "_c_echo", lambda *a, **k: False)
        (tmp_path / "a.dcm").write_bytes(b"DICM")

        with caplog.at_level(logging.INFO, logger="xnatctl.services.upload.dicom_store"):
            with pytest.raises(RuntimeError, match="C-ECHO failed"):
                self._service().upload_dicom_store(
                    dicom_root=tmp_path, host="scp.example.org", called_aet="AET"
                )

        assert any("unencrypted" in r.getMessage() for r in caplog.records)


class TestCLI:
    def test_the_options_are_documented(self) -> None:
        from click.testing import CliRunner

        from xnatctl.cli.main import cli

        result = CliRunner().invoke(cli, ["session", "upload-dicom", "--help"])

        assert "--tls" in result.output
        assert "--tls-ca-bundle" in result.output
        assert "--tls-cert" in result.output
        assert "--tls-key" in result.output

    def test_a_key_without_a_cert_is_refused(self, tmp_path: Path) -> None:
        from conftest import make_authenticated_cli

        key = tmp_path / "k.pem"
        key.write_text("x")
        src = tmp_path / "src"
        src.mkdir()

        result = make_authenticated_cli().invoke(
            [
                "session",
                "upload-dicom",
                str(src),
                "--host",
                "h",
                "--called-aet",
                "A",
                "--tls",
                "--tls-key",
                str(key),
            ]
        )

        assert result.exit_code != 0
        assert "--tls-key requires --tls-cert" in result.output

    def test_tls_material_without_tls_is_refused(self, tmp_path: Path) -> None:
        from conftest import make_authenticated_cli

        cert = tmp_path / "c.pem"
        cert.write_text("x")
        src = tmp_path / "src"
        src.mkdir()

        result = make_authenticated_cli().invoke(
            [
                "session",
                "upload-dicom",
                str(src),
                "--host",
                "h",
                "--called-aet",
                "A",
                "--tls-cert",
                str(cert),
            ]
        )

        assert result.exit_code != 0
        assert "no effect without --tls" in result.output
