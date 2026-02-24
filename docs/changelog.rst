Changelog
=========

0.1.2 (2026)
-------------

**Bug fixes**

- Handle 409 Conflict when creating a resource that already exists (``session upload-exam --attach-only``)
- Tolerate missing resource IDs and non-numeric counts in resource responses
- Skip experiment lookup when session has no resources
- Scope gradual upload clients and reject duplicate files
- Validate exam root directory and sort scan classification
- Use ``files`` input for codecov-action v5

**Features**

- ``session upload-exam`` command for uploading scanner exam-root directories (DICOM + top-level resources)
- Wait for archived session before attaching resources in ``upload-exam``
- Gradual DICOM upload from explicit file lists
- Exam-root classification for mapping directory structure to XNAT resources
- CI: harden security (SHA-pinned actions, minimal permissions, ``persist-credentials: false``)
- CI: cross-platform test matrix (Ubuntu 3.11/3.12/3.13, macOS 3.12, Windows 3.12)
- CI: macOS arm64 binary build
- CI: uv caching, mypy caching, ``alls-green`` gate job
- Batch upload helper script with YAML-driven folder-to-label contract (``scripts/upload_from_folders.py``)

**Docs**

- Add DICOM utilities page documenting ``xnatctl[dicom]`` commands
- Add administration page (catalog refresh, user management, audit log)
- Rewrite all user-facing documentation for beginner-friendly onboarding
- Add shell completion setup instructions
- Document ``session upload-exam`` and upload method comparison

**Refactoring**

- Rename ``admin user add-to-groups`` to ``admin user add``

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
