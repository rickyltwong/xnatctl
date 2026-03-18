# Changelog

All notable changes to this project will be documented in this file.

## 0.2.3 - 2026-03-18

- Fixed gradual DICOM uploads to ignore non-DICOM sidecar files such as `.txt`
  and `.pdf` across directory, ZIP, and explicit-file upload paths.
- Refactored transfer scan sync to use a two-phase download-then-upload flow for
  more predictable pipelined behavior.
- Fixed transfer XML overlay uploads by stripping the session `label` attribute
  that could trigger HTTP 400 errors on destination imports.
- Added extra debug logging around DICOM import and XML overlay failures during
  transfer troubleshooting.
