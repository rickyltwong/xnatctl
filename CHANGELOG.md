# Changelog

All notable changes to this project will be documented in this file.

## 0.2.6 - 2026-03-24

**CLI simplification**

- Consolidate `--unzip`/`--cleanup` into `--extract`/`--no-extract` on session
  and scan download commands. Hidden backward-compat aliases preserved.
- Consolidate `--gradual`/`--archive-format` into `--mode {tar|zip|gradual}` on
  session upload. Hidden backward-compat aliases preserved.
- Collapse `--wait-for-archive`/`--wait-timeout`/`--wait-interval` into single
  `--wait SECONDS` flag on session upload-exam (0 = skip, default: 900).
- Eliminate `--parallel/--no-parallel` toggle; unify to `--workers` across all
  commands that use `@parallel_options` (scan delete, admin refresh-catalogs,
  project transfer).
- Reserve `-P` for `--project` everywhere (removed from `api --params` and
  `pipeline --param`).
- Reserve `-w` for `--workers` everywhere (removed from `pipeline --wait` and
  `pipeline --watch`).
- Normalize `-E` to `--experiment` on session upload and upload-exam (hidden
  `--session` alias preserved for backward compatibility).
- Add profile operational defaults: `workers`, `overwrite`, `direct_archive`,
  `archive_mode`, `extract`. CLI flags override profile values.
- Hide advanced flags from `--help` (still accepted): `--username`, `--password`,
  `--zip-to-tar`, `--ignore-unparsable`, `--misc-label`, `--calling-aet`,
  `--name`, `--session-resources`, `--dest-url`, `--dest-user`, `--dest-pass`.
- Standardize destructive UX: apply `@confirm_destructive` (adds `--dry-run`) to
  `prearchive delete`, `pipeline cancel`, `config remove-profile`.

**Bug fixes**

- Upload: validate DICOM magic bytes for extensionless files (fixes non-DICOM
  files like `ps` being uploaded via gradual-DICOM).
- Upload: add `--direct-archive` flag to `upload-exam` (was previously missing;
  gradual-DICOM uploads now pass `Direct-Archive` query parameter).

## 0.2.5 - 2026-03-20

- Fixed session token expiry during large gradual-DICOM uploads (100K+ files).
  Workers now auto-refresh the XNAT session on HTTP 401 via a thread-safe
  `_SessionRefresher` that deduplicates concurrent re-authentication requests.
- Added manual PyPI publish workflow (`workflow_dispatch`) as fallback when
  auto-publish is skipped due to pre-existing tags/releases.
- Fixed CI `alls-green` check failing on skipped release jobs (auto-tag,
  publish, binary, etc.) by declaring them as `allowed-skips`.

## 0.2.4 - 2026-03-18

- Fixed `auth login` and `xnatctl whoami` so they resolve the current user from
  dedicated current-user endpoints instead of inferring it from `/data/user`.

## 0.2.3 - 2026-03-18

- Fixed gradual DICOM uploads to ignore non-DICOM sidecar files such as `.txt`
  and `.pdf` across directory, ZIP, and explicit-file upload paths.
- Refactored transfer scan sync to use a two-phase download-then-upload flow for
  more predictable pipelined behavior.
- Fixed transfer XML overlay uploads by stripping the session `label` attribute
  that could trigger HTTP 400 errors on destination imports.
- Added extra debug logging around DICOM import and XML overlay failures during
  transfer troubleshooting.

## 0.2.2 - 2026

**Bug fixes**

- Reconcile experiments deleted from destination during incremental transfer
- Save experiment ID mappings for future reconciliation
- Preserve special characters (colons, brackets) in `api get/put/post/delete` query parameter keys
- Resolve `xsiType` for non-imaging sessions in `session show` scan listing

## 0.2.1 - 2026

**Features**

- Pipelined transfer: overlap DICOM uploads with server-side archiving via background poller thread
- `max_pending_archives` config field to throttle concurrent server-side import jobs

**Bug fixes**

- Reconcile previously-synced subjects deleted from destination
- Use `folderName` (not `name`) for prearchive archive requests in `wait_for_archive`
- Add exception guard around `wait_for_archive` poll loop for transient HTTP error resilience
- Flatten ZIP hierarchy for non-DICOM resource uploads
- Ensure experiment is created when all DICOM uploads fail but DICOM was expected
- Resolve `xsiType` correctly for non-imaging sessions in scan list

## 0.2.0 - 2026

**Features**

- `project transfer` command for cross-instance project synchronisation
- Transfer orchestrator with per-scan pipeline, retry, and verification
- Transfer executor with DICOM-zip import and non-DICOM resource repack
- Discovery service for subjects, experiments, and scans
- Filter engine for xsiType, scan type, and resource label filtering
- XML metadata overlay to preserve session/scan metadata after DICOM import
- Prearchive resolution (READY/CONFLICT) during archive wait
- Scan resource caching across DICOM and non-DICOM transfer phases
- Deferred experiment creation (skip pre-create when DICOM import will create)
- Dest-profile CLI helper for dual-instance configuration

**Bug fixes**

- Handle XNAT timestamps with fractional seconds and missing `last_modified`
- Reject multiple `--resource` values in `scan download`

**Docs**

- Add project transfer command documentation
- Update session downloading guide for multi-resource support

## 0.1.3 - 2026

- Fix server version endpoint (use `/xapi/siteConfig/buildInfo/version`)
- Build Linux binary on manylinux_2_28 for RHEL 8+/AlmaLinux 9 compatibility

## 0.1.2 - 2026

**Bug fixes**

- Handle 409 Conflict when creating a resource that already exists (`session upload-exam --attach-only`)
- Tolerate missing resource IDs and non-numeric counts in resource responses
- Skip experiment lookup when session has no resources
- Scope gradual upload clients and reject duplicate files
- Validate exam root directory and sort scan classification
- Use `files` input for codecov-action v5

**Features**

- `session upload-exam` command for uploading scanner exam-root directories (DICOM + top-level resources)
- Wait for archived session before attaching resources in `upload-exam`
- Gradual DICOM upload from explicit file lists
- Exam-root classification for mapping directory structure to XNAT resources
- CI: harden security (SHA-pinned actions, minimal permissions, `persist-credentials: false`)
- CI: cross-platform test matrix (Ubuntu 3.11/3.12/3.13, macOS 3.12, Windows 3.12)
- CI: macOS arm64 binary build
- CI: uv caching, mypy caching, `alls-green` gate job
- Batch upload helper script with YAML-driven folder-to-label contract (`scripts/upload_from_folders.py`)

**Docs**

- Add DICOM utilities page documenting `xnatctl[dicom]` commands
- Add administration page (catalog refresh, user management, audit log)
- Rewrite all user-facing documentation for beginner-friendly onboarding
- Add shell completion setup instructions
- Document `session upload-exam` and upload method comparison

**Refactoring**

- Rename `admin user add-to-groups` to `admin user add`

## 0.1.1 - 2026

- Fix Windows binary build (venv activation in CI workflow)
- Improve Windows installation docs with PATH setup instructions
- Clarify that install script is Linux/macOS only

## 0.1.0 - 2026

- Uniform `-E`/`-P` options across all session and scan commands
- `-E/--experiment` accepts ID or label (label requires `-P` or profile `default_project`)
- `default_project` profile setting now used as automatic `-P` fallback
- Consistent `metavar=ID_OR_LABEL` and help text on all `-E` options
- PyPI trusted publishing via OIDC (stable releases to PyPI, prereleases to TestPyPI)
- Multi-platform standalone binaries: Linux, macOS, Windows via PyInstaller
- `install.sh` auto-detects OS and architecture
- CLI integration test suite: 150 tests covering all commands
- Service layer unit test suite: 138 tests covering all services

## 0.0.2 - 2025

- Add sequential retry mechanism for failed uploads
- Implement thread-local HTTP client for gradual-DICOM uploads
- Enhance 400 error logging

## 0.0.1 - 2025

- Initial release
- Core CLI commands: project, subject, session, scan, resource, prearchive, pipeline, admin, api
- Profile-based YAML configuration
- httpx-based HTTP client with retry logic
- Parallel download and upload support
- JSON and table output formats
