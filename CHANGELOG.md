# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

**Breaking**

- `DownloadService.download_session` is removed. It was never wired to any
  command (`session download` runs its own engine, now
  `DownloadService.download_session_fast`) and its checksum-verification path
  (`verify=`) was unreachable with it. Library callers should use
  `download_session_fast`, `download_scans`, or `download_resource`.
  (Supersedes the earlier note about its `resume` argument.)
- Three library exceptions are renamed off the stdlib names they shadowed:
  `ConnectionError` -> `XNATConnectionError`, `TimeoutError` ->
  `RequestTimeoutError`, `ValidationError` -> `InputValidationError`. The
  library now raises only the new classes, so `except xnatctl.ConnectionError`
  (and the other two old names) no longer matches a raised error. The old names
  survive as deprecated subclass aliases that emit a `DeprecationWarning` when
  instantiated and will be removed in a later minor release; update `except`
  clauses to the new names.
- Single-target download/upload service methods now raise typed exceptions
  instead of returning a failure summary. `DownloadService.download_resource`
  and `UploadService.upload_resource` return their summary only
  on success (`download_scan` follows suit when a resource label is given;
  with `resource=None` it still returns the multi-scan batch summary); on
  failure they raise -- a typed `XNATCtlError` from the client
  layer (`SessionExpiredError`, `PermissionDeniedError`, `ResourceNotFoundError`,
  ...) passes through untouched, and any other error is wrapped as
  `DownloadError`/`UploadError` with the original exception as `__cause__`.
  Library callers that inspected `summary.success`/`summary.errors` after these
  calls should switch to `try`/`except`. Batch methods (`download_scans`, the
  `upload_dicom_*` family) still return summaries;
  `DownloadSummary` and `UploadSummary` gain `raise_for_status()`, which raises
  `BatchOperationError` on a failed batch and is a no-op on success, mirroring
  `httpx.Response.raise_for_status()`. The CLI is unaffected -- its failure exit
  codes and messages are unchanged.
- `DownloadService.download_session_level_resources` now returns
  `list[tuple[str, Path]]` (the `(label, path)` pairs it actually downloaded)
  instead of an `int` count. Library callers that used the return value only
  as a count should switch to `len(...)`.

**Features**

- `session download` and `scan download` gain `--verify`, which checks every
  downloaded file's MD5 against the server's catalog checksum after the
  download completes -- extracted or still zipped, either way, and covering
  session-level resources too when combined with `--session-resources`. Files
  are matched by scan and resource, not just filename, so same-named files in
  different scans (`1.dcm` in scan 2 and scan 5) are checked independently
  instead of colliding. A mismatch, a file the server listed but that never
  landed locally, or two different files that ambiguously map to the same
  path fails the command with the offending paths printed. A file the server
  has no checksum for is reported as unverifiable rather than silently
  skipped -- and if the server had no checksum for anything at all that was
  downloaded, the command fails outright rather than reporting a pass with
  nothing actually checked.
- DICOM utilities (`xnatctl dicom`, `session upload-dicom` C-STORE) and
  OS-keychain password storage (`config set-password`) now ship in every
  install, including the standalone binary. The `dicom` and `keyring` extras
  are gone; `pip install "xnatctl[dicom]"` now warns about an unknown extra
  and installs the full package.
- One-call library connect. `xnatctl.XNATClient.from_profile("prod")` builds a
  client from a saved config profile, running the same credential resolution the
  CLI does (environment variables over profile config, cached or `XNAT_TOKEN`
  session token, `auto_reauth` on). Entering the client as a context manager
  logs in when a password is available and no token is cached yet. Each resource
  type is reachable through a bound, cached accessor on the client
  (`client.projects`, `client.sessions`, `client.downloads`, ...), and the
  package root now re-exports the client, config, service classes,
  resource/progress models, and the full exception hierarchy as the supported
  public surface.

**Fixes**

- 404 classification no longer depends on error-message text. Services used to
  decide "resource not found" by checking whether the literal substring `"404"`
  appeared in an exception's message, which also fired on any unrelated error
  whose text happened to contain it (a session labelled `SUB404`, for
  instance). The client now enriches every 404 it raises with
  `status_code`/`method`/`path` in `details` -- matching what it already did
  for 401/403 -- and services dispatch on the exception's type instead.
- Downloads now go through the client's retry, auth, and typed-error path. Every
  streamed download (session, scan, resource, and cross-server transfer) used to
  bypass the client and talk to `httpx` directly, so it got no retry ladder, no
  401/403/404 mapping, and -- most visibly -- no basic-auth fallback: a client
  built from a username and password but not yet logged in failed every download
  while plain requests succeeded. Those downloads now reuse the same path as the
  rest of the client.
- Downloads are written atomically. Bytes stream to a temporary `.part` file that
  is renamed into place only after the transfer completes and its byte count
  matches the server's `Content-Length`; a dropped connection can no longer leave
  a truncated file that looks whole, and a size mismatch fails loudly.
- A corrupt session ZIP now fails the command. `session download` used to print
  an "invalid ZIP" note, keep going, and still exit 0; a truncated or corrupt
  archive is now CRC-checked before extraction and aborts with a nonzero exit.
- Cross-server transfer scan imports no longer retry unrecoverable failures.
  The import step used to repeat *any* exception -- a permanent 400 (bad
  project, missing subject), a permission error, even a programming error --
  burning the full backoff ladder before reporting what the first attempt
  already knew. It now retries only transient conditions, and gains the same
  transient-vs-permanent HTTP 400 discrimination the upload paths use, so an
  import-race 400 during a transfer is retried instead of failing the scan.

## 0.3.0 - 2026-08-07

A large hardening release. The themes: credentials stop leaking, failures stop
lying, and Ctrl+C works.

**Breaking**

- `auth login --password <value>`, `session upload --password <value>`, and
  the hidden `--dest-pass <value>` no
  longer accept a password on argv (visible in `ps`, `/proc/*/cmdline`, and
  shell history). Passing a value is now a usage error (exit 2). Use
  `--password-stdin` / `--dest-pass-stdin`, the `XNAT_PASS` env var, stored
  profile credentials, or the interactive prompt.
- **stdout is data only.** Success lines, progress bars, confirmation prompts,
  dry-run previews, and the "No results" notice now go to stderr. Piping
  `-o json` no longer interleaves status text with the JSON, and redirecting
  stdout no longer kills the progress bar. A script that captured progress
  text from stdout must read stderr instead.
- **Exit codes are differentiated.** Failures used to exit 1 uniformly; they
  now exit 3 (auth), 4 (network), 5 (not found), 6 (permission), 7 (cancelled),
  or 1 (general). Click keeps 2 for usage errors. Codes only ever became *more*
  specific, so `!= 0` tests are unaffected — `== 1` tests are not.

**Features**

- **TLS for DICOM C-STORE.** `session upload-dicom` sent pixel data and its
  identifiers in cleartext with no way to encrypt them. `--tls` turns on
  verified TLS; `--tls-ca-bundle`, `--tls-cert`, and `--tls-key` cover private
  CAs and mutual TLS, each with an `XNAT_DICOM_TLS*` environment equivalent.
  There is deliberately no "skip verification" mode. Plaintext transfers now
  log that they were unencrypted.
- **Passwords can live in the OS keychain.** `xnatctl config set-password`
  stores the password via `keyring` and writes `password_source: keyring` to
  the profile instead of a plaintext `password`. Needs the `xnatctl[keyring]`
  extra.
- **Ctrl+C stops a parallel transfer.** It previously had to wait out every
  in-flight retry backoff first — up to ~50 seconds. Workers now share a
  cancellation token and stop within a fraction of a second.
- **A local audit trail.** Destructive commands append a JSON line to
  `~/.config/xnatctl/audit.log` (mode 0600, rotates once at 10 MB) recording
  the command, its targets, and whether it was a dry run.
- **`--verbose` diagnoses things.** The client, auth, and service layers emit
  structured diagnostics, and `XNATCTL_DEBUG=1` adds full tracebacks plus an
  httpx wire trace. Secrets are redacted on the way out.
- **`resource` commands work at every hierarchy level.** `resource
  download|upload|list` were experiment-only and returned a 500 when handed a
  project ID. They now take `-P/--project` and `-S/--subject`.
- **Actionable errors.** Failures print one line plus a suggested next step
  instead of a traceback — including on the `auth` commands, which previously
  bypassed the shared error path entirely and so never showed the hints.
- **Deprecated flags now say when they die.** Warnings name the removal
  release (`--unzip is deprecated and will be removed in 0.5.0; use --extract
  instead`) and go to stderr. Three flags that were accepted in total silence
  now warn, and `--include-resources` — which warned through a
  `DeprecationWarning` Python hides by default — is visible at last. The
  policy is documented in the new Stability page.
- Shell completion emits Click's own scripts, so `xnatctl proj<TAB>` completes
  to `project` rather than `plain,project`.
- New documentation: a Stability and Deprecation Policy page (what scripts may
  bind to, and for how long), a Performance page (measured throughput and peak
  RSS for the transfer paths), and `docs/adr/` recording ten decisions that
  look like mistakes without their context.

**Fixes**

- **Single-archive `session upload` could not tell a transient import 400 from
  a permanent one.** It wrapped the core client -- which raises a typed error
  on 400 before the upload retry helper can inspect the body -- so one
  concurrent-modification 400 (routine when two uploads race the same session)
  failed the command immediately instead of being retried. The path now uses
  the same raw uploader as batch uploads: transient 400s retry,
  misconfigurations fail on the first attempt, failures keep the
  differentiated exit codes, and a session evicted mid-upload is refreshed and
  retried instead of failing.
- **A `ca_bundle` profile only protected the core client.** The raw HTTP
  clients behind uploads and parallel session download fell back to plain
  verification, so a self-signed XNAT that worked for listings failed TLS on
  transfers. All raw clients now inherit the profile's CA bundle.
- **`scan` commands on a project-scoped URL addressed the session, not the
  scan.** XNAT ignores sub-resource suffixes under
  `/data/projects/{P}/experiments/{E}`, answering with the parent experiment
  document — so `scan list -P` reported nothing and `scan delete -P` aimed its
  DELETE at the whole session. Scan URLs are now built in a form XNAT routes,
  and a request that cannot be made safe is refused with an explanation.
- **Download checksum verification never compared anything.** It fetched the
  session-level file listing while the download took scan files; the two sets
  do not overlap, so every file was skipped and the function returned its
  initial `True`. Verified against a live server: 12 files listed against 3112
  downloaded.
- **`prearchive archive|rebuild|move` claimed success regardless.** XNAT
  reports these failures in the body of an HTTP 200, so all three now inspect
  the body before reporting success.
- **Pagination could loop forever.** `XNATClient.paginate()` on an endpoint
  XNAT does not paginate — `/data/projects` among them — advanced the offset
  indefinitely and re-yielded every row each pass. Found by the new integration
  tier at offset 151450.
- **No httpx exception escapes the client.** Raw `httpx` errors used to reach
  users as tracebacks. Every failure is now an `XNATCtlError` subtype, checked
  by walking httpx's whole exception tree rather than spot-checking names —
  which is how `TooManyRedirects`, `LocalProtocolError`, and `CloseError` were
  found still leaking.
- **Retries got smarter.** 429 and `Retry-After` (both delta-seconds and
  HTTP-date) are honoured with a 300s cap; backoff has full jitter so parallel
  workers stop re-stampeding a struggling server; and a request that failed
  *after* being sent is only retried when the method is idempotent.
- **Uploads stop retrying 400s that retrying cannot fix.** A misconfigured
  upload burned 403 attempts over half an hour before failing; permanent
  import errors are now recognised and fail in about 5 attempts and under a
  second, while genuinely transient ones still retry.
- **A session that expires mid-transfer no longer fails the transfer.** Worker
  threads propagate a refreshed token back to the shared client, and cached
  sessions expire on idle time rather than from creation — matching how XNAT
  actually retires a JSESSIONID.
- **Connect failures fail in ~10 seconds, not 6 hours.** The single timeout
  covering both connect and read is split; the generous ceiling now applies
  only to reads, where large DICOM transfers need it.
- **Credentials stop leaking into files and logs.** The session cache and
  `config.yaml` are created owner-private (0600) atomically rather than
  world-readable-then-fixed, URL userinfo and query secrets are redacted
  everywhere including logger output, and a world-readable config holding a
  plaintext password now warns.
- **Disabled TLS is impossible to miss.** `verify_ssl: false` prints a warning,
  `XNAT_VERIFY_SSL` is parsed strictly instead of treating any value as true,
  and a new `ca_bundle` profile field offers the secure alternative for
  self-signed certificates.
- Failed uploads and downloads exit non-zero under `-o json` instead of
  reporting failure in the payload and success in the exit code.
- `whoami` and `health ping` honour `--profile`, `-o`, and `-q`.
- Temporary archives are no longer left behind by `resource upload`.
- Windows: file modes are no longer treated as meaningful there, where
  `os.stat` reports 0666/0777 regardless of the actual ACL.
- Help text no longer rewraps example blocks into unreadable paragraphs.

## 0.2.11 - 2026-07-21

**Fixes**

- `scan list` / wildcard `scan delete`: list scans of sessions whose scans are
  `xnat:otherDicomScanData` (e.g. `xnat:optSessionData` / OCT). The scans
  endpoint is now queried unfiltered first; a guessed `xsiType` filter (which a
  session's type cannot reliably derive) is only used as a fallback when the
  unfiltered listing is empty. (#16)
- `api put`: writing a file to a resource endpoint
  (`/resources/<label>/files/<name>`) now auto-sets `?inbody=true` for a raw
  body, so the obvious command works instead of failing with an opaque
  400/500. An explicit `inbody` is never overridden, and `inbody=true` with no
  body is now an actionable error instead of a silent empty PUT. (#18)
- `resource upload` (`ResourceService.upload_file`): set `inbody=true` and
  stream the body via httpx `content=` for resource file writes, matching the
  gradual-DICOM upload path. Adds a `content=` passthrough to `XNATClient`. (#21)
- `prearchive archive`: transparently re-authenticate on a mid-command session
  expiry (a slow archive that outlasts the ~15-min JSESSIONID no longer reports
  a successful archive as a 401 failure), and map the archive 404 to an
  idempotency-aware error that names the session and explains it may already be
  archived. The CLI client now enables `auto_reauth` when a password is
  available. (#20)
- `prearchive move`: add a regression test locking in that XNAT's `301` move
  redirect is treated as success (the client has followed redirects since the
  initial commit). (#19)

## 0.2.10 - 2026-07-08

**Fixes**

- `session upload-exam`: raise the default `--wait` (seconds to wait for
  archiving before attaching resources) from 900s to 4 hours via the new
  `DEFAULT_ARCHIVE_WAIT_SECONDS` constant. Large sessions (100k+ files) can
  take well over 15 minutes to archive; the old default let the wait expire
  before resource attachment.
- `session upload-exam`: on archive-wait timeout, no longer hard-aborts and
  discards the successful DICOM upload. It now keeps the DICOM result, reports
  the unattached resources, and prints the exact `--attach-only` command to
  re-run once archiving completes, so session resources/misc files are never
  silently dropped. Under `-o json` the `resources` object gains
  `attached`, `pending`, `reason`, and `rerun` fields.

## 0.2.9 - 2026-05-14

**Features**

- Add `xnatctl xsync` subcommand group for XSync plugin operations:
  `refresh-credentials`, `list`, `setup`, `status`, `history`, `progress`,
  `sync`, and `sync-subject`. The `refresh-credentials` orchestrator
  composes the three-step XSync token-rotation flow
  (`remoteREST` -> `credentials/save` -> `credentials/check`) entirely
  inside `xnatctl`, removing the need to drop to raw `curl`. Remote
  passwords are sourced from `--remote-pass-stdin`, the
  `XNAT_XSYNC_REMOTE_PASS` env var, or interactive prompt; passing
  `--remote-pass <secret>` on argv is rejected at parse time as a
  `click.UsageError` so secrets never reach process memory or
  `~/.bash_history`. (closes #15)
- Promote `--verbose/-v`, `--profile/-p`, `--output/-o`, and `--quiet/-q`
  to root-group options on the `xnatctl` command. They now work as
  `xnatctl --verbose api get ...`; the existing per-subcommand variants
  still work and win when both are set explicitly. `--version` remains
  eager and is not shadowed by `-v`. (closes #14)
- `xnatctl api get -o json` no longer hard-errors on non-JSON response
  bodies. Instead it emits a single-line stderr warning and writes the
  raw body to stdout (text-decoded when UTF-8-safe, raw bytes
  otherwise). Exit code stays 0 when the underlying HTTP call
  succeeded. Useful against `/xapi/xsync/progress/{projectId}` and
  similar text/plain endpoints. (closes #13)

## 0.2.8 - 2026-05-07

**Features**

- Add `xnatctl resource refresh URI [--options ...]` for targeted catalog
  refresh against `/data/services/refresh/catalog`. Options accept any of
  `checksum`, `delete`, `append`, `populateStats`. Replaces the need to drop
  to `api post` for single scan/resource catalog refreshes.
- Add `--project/-P` to `xnatctl resource upload` so label-based session
  resolution works for scan-level and session-level resource uploads.
  Default falls back to the active profile's `default_project`.
- Allow `xnatctl config show -p NAME` to filter the output to a single
  profile (and its details). Unknown profile names error with the available
  list. Unfiltered behavior is unchanged.
- Add `HierarchyService` for shared path building, response-envelope
  parsing (`ResultSet.Result` and `items[]`), and resolution of
  `ProjectRef` / `SubjectRef` / `ExperimentRef` / `ScanRef` / `ResourceRef`.
  Services and CLI commands now route through it instead of open-coding
  paths and parsing.
- Bundle `pydicom` and `pynetdicom` in the standalone PyInstaller binary
  so `xnatctl dicom` utilities work out of the box after `install.sh`.

**Bug fixes**

- Fix `session show` and `scan list` returning "Session not found" or
  "No results" for sessions reachable via subject-scoped paths.
  `HierarchyService.resolve_experiment` now falls back to listing
  `/data/projects/{P}/experiments?columns=ID,label,subject_ID,xsiType`
  with client-side label/ID match when the direct project-experiment
  endpoint fails, and tries `/data/experiments/{ID}` for accession-ID-shaped
  references that don't resolve in the active project.
- Narrow `_inspect_experiment` exception swallowing in `cli/scan.py` to
  `ResourceNotFoundError` only; transient network and auth errors now
  surface to the user instead of silently producing "No results".
- Fix `xnatctl api put -f FILE` and `api post -f FILE` corrupting binary
  files with `UnicodeDecodeError`. The body is now read in binary, with a
  UTF-8 + JSON probe before falling back to raw bytes. JSON and plain-text
  paths are unchanged on the wire.
- Fix `session upload --mode gradual --prearchive` silently uploading to
  direct-archive: the CLI wrapper now threads `direct_archive` through to
  the gradual-DICOM service. Consolidate prearchive routing through a
  single `archive_destination_params()` helper that uses
  `dest=/prearchive/projects/{p}` instead of `Direct-Archive=false`. Help
  text now warns that `--prearchive` is best-effort against projects with
  auto-archive enabled.
- Fix `subject merge` silently destroying experiments. Replace the global
  `PUT /data/experiments/{id}?xnat:experimentData/subject_ID=...` shortcut
  with the scoped `PUT /data/projects/{p}/subjects/{target_id}/experiments/{id}`
  that the XNAT web UI uses, plus per-experiment post-PUT verification
  before the source-subject delete. `SubjectService.delete()` now refuses
  to delete subjects with experiments still attached unless `force=True`.
- De-flake `tests/test_archive_poller_zero_vs_error` on slower CI runners
  by waiting on the actual observable (`zero_scan_cycles >= 1`) rather
  than the racy proxy.
- Various resource-listing and transfer edge-case fixes carried into the
  HierarchyService refactor.

**Documentation**

- Document that `xnatctl resource upload` PUTs files directly to the
  resource catalog and **bypasses XNAT project-level DICOM anonymization
  scripts and pipelines**. Use `xnatctl session upload` /
  `xnatctl session upload-exam` when anonymization is required. Disclosure
  appears in `--help` and `docs/uploading.rst`.

**Refactors**

- Extract `cli/common.py` context helpers (`get_profile`,
  `default_project_from_context`, `require_project_from_context`,
  `resolve_workers_from_context`) and adopt them across CLI commands.
- Migrate services and CLI from open-coded `ResultSet.Result` / `items[]`
  parsing to `HierarchyService.extract_rows` / `extract_first_item`.
  `Session.get()` now merges `meta["xsi:type"]` into the model when the
  field-level value is missing (fixes non-imaging session xsiType).
  Behavior note: a session label that cannot be resolved in a project
  now raises `ResourceNotFoundError` (was `ValueError`).

## 0.2.7 - 2026-04-20

**Features**

- Add `xnatctl dicom modify` for batch in-place DICOM tag editing across single
  files or directories, with repeatable `--tag KEYWORD=VALUE`, recursive search,
  `--backup`, `--dry-run`, atomic writes, and JSON/table output modes.

**Bug fixes**

- Switch prearchive archiving to XNAT's archive service API
  (`/data/services/archive`) for both `prearchive archive` and transfer-side
  prearchive resolution.
- Surface archive-service failures during transfer instead of letting them
  degrade into silent archive wait timeouts; blocking fallback paths now record
  per-experiment archive errors consistently.
- Encode archive-service path segments for project, timestamp, session, subject,
  and experiment values to avoid malformed archive and prearchive-delete
  requests.

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
