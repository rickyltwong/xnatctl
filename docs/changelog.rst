Changelog
=========

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
