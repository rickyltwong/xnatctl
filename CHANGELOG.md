# Changelog

All notable changes to this project will be documented in this file.

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
