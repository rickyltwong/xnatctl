Changelog
=========

0.1.1 (2026)
-------------

- Fix Windows binary build (venv activation in CI workflow)
- Improve Windows installation docs with PATH setup instructions
- Clarify that install script is Linux/macOS only

0.1.0 (2026)
-------------

- Uniform ``-E``/``-P`` options across all session and scan commands
- ``-E/--experiment`` accepts ID or label (label requires ``-P`` or profile ``default_project``)
- ``default_project`` profile setting now used as automatic ``-P`` fallback
- Consistent ``metavar=ID_OR_LABEL`` and help text on all ``-E`` options
- PyPI trusted publishing via OIDC (stable releases to PyPI, prereleases to TestPyPI)
- Multi-platform standalone binaries: Linux, macOS, Windows via PyInstaller
- ``install.sh`` auto-detects OS and architecture
- CLI integration test suite: 150 tests covering all commands
- Service layer unit test suite: 138 tests covering all services

0.0.2 (2025)
-------------

- Add sequential retry mechanism for failed uploads
- Implement thread-local HTTP client for gradual-DICOM uploads
- Enhance 400 error logging

0.0.1 (2025)
-------------

- Initial release
- Core CLI commands: project, subject, session, scan, resource, prearchive, pipeline, admin, api
- Profile-based YAML configuration
- httpx-based HTTP client with retry logic
- Parallel download and upload support
- JSON and table output formats
