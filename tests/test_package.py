"""Tests for xnatctl package imports and exports."""

from __future__ import annotations


class TestPackageImports:
    """Tests for package imports."""

    def test_import_xnatctl(self):
        import xnatctl

        assert hasattr(xnatctl, "__version__")

    def test_import_core_modules(self):
        from xnatctl.core import auth, client, config, exceptions, logging, output, validation

        assert client is not None
        assert config is not None
        assert auth is not None
        assert exceptions is not None
        assert validation is not None
        assert output is not None
        assert logging is not None

    def test_import_models(self):
        from xnatctl.models import base, progress, project, resource, scan, session, subject

        assert base is not None
        assert project is not None
        assert subject is not None
        assert session is not None
        assert scan is not None
        assert resource is not None
        assert progress is not None

    def test_import_services(self):
        from xnatctl.services import (
            admin,
            base,
            downloads,
            pipelines,
            prearchive,
            projects,
            resources,
            scans,
            sessions,
            subjects,
            uploads,
        )

        assert base is not None
        assert projects is not None
        assert subjects is not None
        assert sessions is not None
        assert scans is not None
        assert resources is not None
        assert downloads is not None
        assert uploads is not None
        assert prearchive is not None
        assert pipelines is not None
        assert admin is not None

    def test_import_cli(self):
        from xnatctl.cli import (
            admin,
            api,
            auth,
            common,
            config_cmd,
            dicom_cmd,
            main,
            pipeline,
            prearchive,
            project,
            resource,
            scan,
            session,
            subject,
        )

        assert main is not None
        assert common is not None
        assert auth is not None
        assert config_cmd is not None
        assert project is not None
        assert subject is not None
        assert session is not None
        assert scan is not None
        assert resource is not None
        assert prearchive is not None
        assert pipeline is not None
        assert admin is not None
        assert api is not None
        assert dicom_cmd is not None


class TestExceptionHierarchy:
    """Tests for exception hierarchy."""

    def test_xnat_error_base(self):
        from xnatctl.core.exceptions import XNATCtlError

        exc = XNATCtlError("test error")
        assert "test error" in str(exc)
        assert isinstance(exc, Exception)

    def test_auth_error(self):
        from xnatctl.core.exceptions import AuthenticationError

        exc = AuthenticationError("https://example.org", "bad creds")
        assert "example.org" in str(exc)

    def test_network_error(self):
        from xnatctl.core.exceptions import NetworkError

        exc = NetworkError("https://example.org", "connection failed")
        assert "example.org" in str(exc)

    def test_validation_errors(self):
        from xnatctl.core.exceptions import (
            InvalidIdentifierError,
            InvalidPortError,
            InvalidURLError,
            PathValidationError,
        )

        url_exc = InvalidURLError("bad-url", "missing scheme")
        assert "bad-url" in str(url_exc)

        port_exc = InvalidPortError(99999)
        assert "99999" in str(port_exc)

        id_exc = InvalidIdentifierError("project", "bad@id", "invalid chars")
        assert "project" in str(id_exc)
        assert "bad@id" in str(id_exc)

        path_exc = PathValidationError("/bad/path", "does not exist")
        assert "/bad/path" in str(path_exc)
