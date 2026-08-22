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
- `xnatctl.services.uploads` is gone; `UploadService` and its module-level
  helpers (`collect_dicom_files`, `upload_archive_or_raise`,
  `upload_single_archive`, `SessionRefresher`, `ArchiveUploadResult`,
  `DICOMStoreSummary`, `build_dicom_tls_context`, `split_into_batches`,
  `split_into_n_batches`) now live in `xnatctl.services.upload`, split across
  one module per transport (`rest_batch`, `gradual`, `dicom_store`,
  `resources`). Library callers importing from the old module path should
  update to `xnatctl.services.upload` (or `import xnatctl; xnatctl.UploadService`,
  which is unaffected). CLI behavior is unchanged.
- `-o json` output changes shape on five commands that already shipped
  structured JSON: `scan download`, `session upload` (single-archive and
  directory-batch REST transport), `session upload-dicom` (C-STORE), and
  `session upload --mode gradual`. Every transfer command now prints one
  `TransferSummary` object -- `operation`, `session_id`, `project`,
  `output_dir`/`source`, `scans`, `files`, `bytes`, `duration_seconds`,
  `status` (`"success"`/`"partial"`/`"failed"`, always agreeing with the exit
  code), `items` (per-item results, each `{"id", "status", "error"}`), and,
  with `--verify`, a nested `verification` report. `files`/`bytes` are only
  ever the count/size actually transferred, never an attempted-but-maybe-
  failed approximation -- `null` where the underlying summary doesn't track
  that distinction. No compatibility shim is kept; scripts reading the old
  top-level keys below must update. Field mapping, old -> new:
  - `scan download`: `output_path` -> `output_dir`; `success` (bool) ->
    `status` (enum); `total_size_mb` -> `bytes` (now integer bytes, and only
    present when the download itself succeeded, independent of
    `--verify`); `errors` -> `items[0].error`; `verification` keeps its
    inner shape, now nested the same way it always was.
  - `session upload` (single archive): `success`/`file`/`session` ->
    `status`/`source`/`session_id`, plus new `files` and `bytes`.
  - `session upload` (directory batch): `success` -> `status`;
    `total_files`/`total_size_mb` -> `files`/`bytes`, `null` unless every
    batch succeeded (the old fields counted files *attempted* across all
    batches, not transferred); `batches_succeeded`/`batches_failed` collapse
    into one `items[0]` entry (`id: "batches"`) since `errors` was never
    keyed to a specific batch; `errors` -> `items[0].error` (joined).
  - `session upload-dicom` (C-STORE): `success`/`total_files`/`sent`/`failed`
    -> `status`/`files` (now `sent`, the service's own successfully-sent
    count, not the scanned total)/dropped/`items[0].error`.
  - `session upload --mode gradual`: `success`/`total`/`succeeded`/`failed`/
    `errors` -> `status`/dropped/dropped/dropped/`items[0].error`; `files`
    is `null` unless the run fully succeeded.

  `session download`, `resource upload`, and `resource download` gaining
  `-o json` output is net-new, not a schema change (see Features).
  `session upload-exam`'s existing, separately documented `-o json` contract
  is unaffected.

**Features**

- **User lifecycle administration.** `admin user` gains `list` (`--active`
  for only signed-in accounts), `show`, `enable`/`disable`, `roles`
  (`--grant`/`--revoke` a site-wide role, or list with neither), `groups`
  (list a user's group memberships), `remove` (drop a user from a project's
  groups), and `kill-sessions` -- the fix-command for a shared/service
  account that has exhausted its concurrent-session limit and started
  failing every new login with 401s.
- **Project access and membership.** `project` gains `users` (list members
  and roles), `grant`/`revoke` (owner/member/collaborator role management --
  a user found in more than one role is revoked from all of them), `access`
  (get, or `--set public|protected|private`), and `requests PROJECT`
  (read-only listing of a project's access requests, pending and resolved).
  Approval/denial is intentionally not offered: XNAT resolves a project
  access request against whichever account is signed in when the call is
  made, not the account the request was addressed to, so there is no safe
  way for an admin to accept or decline one on another user's behalf.
- **Site config, plugins, and server info.** `admin site-config get [KEY]`
  and `set KEY VALUE` read/write XNAT site configuration; `admin plugins`
  lists installed plugins (`plugins show ID` for one); `admin version`
  prints server build/version information (`-q` for the bare version string).
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
  nothing actually checked. `-o json`'s standalone `{"verification": {...}}`
  block is unreleased, so folding it into `TransferSummary` (see Breaking)
  is not itself a breaking change.
- `session download`, `resource upload`, and `resource download` gain
  structured `-o json` output for the first time -- previously they printed
  no JSON at all outside table mode. See the Breaking entry above for the
  shared `TransferSummary` shape and the Downloading/Uploading docs pages
  for the full schema.
- DICOM utilities (`xnatctl dicom`, `session upload-dicom` C-STORE) and
  OS-keychain password storage (`config set-password`) now ship in every
  install, including the standalone binary. The `dicom` and `keyring` extras
  are gone; `pip install "xnatctl[dicom]"` now warns about an unknown extra
  and installs the full package.
- One-call library connect. `xnatctl.XNATClient.from_profile("prod")` builds a
  client from a saved config profile, running the same credential resolution the
  CLI does (environment variables over profile config, cached or `XNAT_TOKEN`
  session token, `auto_reauth` on). Entering the client as a context manager
  logs in when a password is available and no token is cached yet. Each core
  resource type is reachable through a bound, cached accessor on the client
  (`client.projects`, `client.subjects`, `client.sessions`, `client.scans`,
  `client.resources`, `client.downloads`, `client.uploads`,
  `client.prearchive`, `client.pipelines`, `client.admin`, `client.hierarchy`,
  `client.exam_uploads`), and the package root now re-exports the client,
  config, service classes,
  resource/progress models, and the full exception hierarchy as the supported
  public surface. The top-level `xnatctl` namespace is now **Stable** and
  semver-covered, superseding its earlier Provisional status;
  `xnatctl.core.*`/`xnatctl.services.*` internals reached by importing the
  submodule directly stay Provisional.
  README gains a "Use as a Python library" quickstart and exception-handling
  example.

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
- The service layer now defends its own REST paths instead of trusting the
  CLI to have validated them first. Every caller-supplied identifier
  (project/subject/session/scan/resource IDs and labels) is percent-encoded
  at the point it enters a URL path, and the hierarchy refs
  (`ProjectRef`/`SubjectRef`/`ExperimentRef`/`ScanRef`/`ResourceRef`) reject
  a value containing `/`, `\`, `?`, `#`, `%` (`#`/`?`/`%` are still allowed in
  a *resource* label specifically, since those are routine there and the
  quoting layer encodes them unambiguously), a segment made up entirely of
  dots (`.`/`..`), control characters (including the Unicode C1 range), or
  leading/trailing whitespace, as soon as they are constructed. This matters
  now that the library surface is public: a caller building a ref directly
  (bypassing the CLI's own input validation) could previously redirect a
  request to a different endpoint, e.g. an experiment ID of
  `SUB1/experiments/XNAT_E1?activate=`, or a project ID of `..`.
  IDs limited to alphanumerics/dots/dashes/underscores are unaffected: their
  URLs are byte-for-byte unchanged. An ID or label containing a character
  that is reserved in a URL (a space, parentheses, `+`, etc.) is now
  *consistently* percent-encoded everywhere -- previously most call sites
  built the URL by raw string interpolation and sent that character through
  unencoded, which the server still accepted for many routes but not all
  (and was never intentional API surface), so this is not always a
  byte-for-byte-identical URL, only a request-equivalent one once decoded.
  An empty or whitespace-only segment is rejected the same way -- it used to
  build a *collection* route instead of failing (`ProjectService.delete("")`
  silently became `DELETE /data/projects/`, not an error on a missing ID),
  and the low-level path joiner no longer silently strips a leading/trailing
  `/` from a part either (`"/TARGET"` used to canonicalize to `"TARGET"`, a
  different resource for what was actually invalid input; a bare `"/"` used
  to collapse to an empty segment, a double-slash route) -- both now raise.

  Three related fixes ship alongside: `DownloadService.download_scans`'
  multi-scan batch request now percent-encodes each scan ID individually
  before joining them with the literal `,` XNAT's batch syntax requires,
  instead of joining first and encoding the whole thing afterward (which
  would have percent-encoded the delimiter itself, breaking the batch
  request), and it now rejects an empty scan-ID list or an empty ID within it
  up front. A caller-supplied local download filename (`zip_filename`) is
  checked to ensure it cannot resolve outside the requested output
  directory. And a server-reported scan ID or resource label used to name a
  local file or folder -- both in `DownloadService` and in ZIP extraction
  (`_extract_scan_zip`, where the same label also doubles as the local
  extraction root for that resource) -- is now rejected outright unless it
  is already its own safe path component; it is no longer reduced to a
  generic fallback name, which could let two different hostile values alias
  onto (and overwrite) the same local destination.

  That local-path check (`validate_local_path_component`) now also accounts
  for Windows and macOS filesystem behavior, not just POSIX -- this package
  is CI-tested on Windows. A value containing `:` is rejected (a
  drive-qualified/drive-relative value like `C:escape` has no `/` or `\` at
  all, so the separator check alone missed it, and joining it onto a base
  path on Windows discards the base entirely, escaping containment before
  the result is even resolved; this also closes the NTFS
  alternate-data-stream form `file:stream`). A value starting or ending with
  a dot or space is rejected (Windows silently strips a trailing dot/space,
  so `"scan."` and `"scan"` land on the same real file there even though
  they're different strings here; a dot/space in the interior is unaffected).
  A Windows-reserved device basename (`CON`, `PRN`, `AUX`, `NUL`,
  `COM1`-`COM9`, `LPT1`-`LPT9`, case-insensitive, matched against the stem
  regardless of extension) is rejected. And a value not already in Unicode
  NFC form is rejected, since an NFD-decomposed value can be byte-distinct
  here while denoting the same filename on a normalizing filesystem
  (macOS/HFS+) -- real XNAT-reported values are ASCII in practice, so this
  cannot reject anything genuine.

  Separately: an explicitly-supplied empty string is no longer treated the
  same as an omitted (`None`) filter anywhere the two used to be conflated.
  `HierarchyService.build_subject_collection_path("")` and
  `build_experiment_collection_path(project, "")` used to silently widen to
  the unfiltered/site-wide collection instead of failing on the bad ID;
  `DownloadService.download_scans(resource="")` and
  `download_resource(scan_id="")` used to silently widen to "all resources"
  and "session-level, unscoped" respectively; and
  `download_session_fast`/`build_verification_manifest`'s
  `include_resources=("",)` (a non-empty tuple containing an empty string)
  used to pass the tuple's own truthiness check and then fall through a
  later per-item check into an unfiltered request. All of these now raise
  instead of degrading to the broader scope.

  Two follow-up sweeps closed the same two defect classes wherever else they
  showed up. First, the empty-string-widening sweep now also covers
  `ResourceService`'s legacy `(session_id, scan_id, project)` triple --
  `scan_id=""` used to silently widen to the SESSION-level resource on
  every method that takes it, including `delete()` (which defaults
  `remove_files=True`) -- plus the equivalent scan/project checks in
  `SessionService`, `SubjectService`, `PipelineService`,
  `PrearchiveService`, `AdminService`, and the standalone resource-upload
  path, and `resource_filter=""` widening a verification run to every
  resource on a scan. Second, every remaining place a server-reported or
  caller-supplied identifier was joined onto a local path without going
  through the Windows/macOS-aware validity check now does: cross-server
  transfer staging (`TransferExecutor`, scan-worker temp directories),
  `DownloadService.download_resource`'s extraction root (previously built
  from the raw label, so `"C:escape"` reached Windows path handling before
  any check ran), and the exam-upload misc-files ZIP name. The CLI's
  `--name` flag on `session download`/`scan download` now runs the same
  full check instead of a separator-only one.

  `validate_local_path_component` itself also grew two checks: the
  Windows-invalid filename characters `< > " | ? *` are rejected (in
  addition to `:`, added previously) on every platform, not only when
  actually running on Windows, so a value that is legal on XNAT does not
  fail only on some download machines. And a new
  `check_no_casefold_collision` helper -- not folded into
  `validate_local_path_component` itself, since case is a property of how
  two values compare, not of either one alone -- catches the case where two
  individually-valid sibling values (two scan IDs in one session download,
  two session-resource labels) would collide on a case-insensitive
  filesystem (Windows, and macOS/HFS+ by default): `"scan"` and `"SCAN"`
  are both fine on their own, but the second one to be created now raises
  instead of overwriting the first. The same check now also applies WITHIN
  a single downloaded scan ZIP: two members whose resource labels differ
  only by case (`"DICOM"` then `"dicom"`) raise instead of being extracted
  into the same merged directory -- the same literal label recurring across
  many members of one resource, which is the normal case, is unaffected.

  Two more empty-string-widening sites, of the same class as before but
  lower severity (a wider *read*, not a different write target, since these
  are query-param values httpx already encodes safely): `AdminService`'s
  `audit_log`/`get_xapi_audit` filters and
  `SessionService.list_project_experiment_rows`'s subject filter now also
  reject an explicitly-empty string instead of silently returning more rows
  than asked for.

  A further round closed five more instances of the same two defect
  classes. Cross-server scan transfer now runs a casefold preflight over
  the whole batch of scan IDs before any download worker starts (two scan
  IDs differing only by case, e.g. `"1a"`/`"1A"`, used to share the same
  local staging directory once workers wrote into it concurrently), and a
  second casefold check per scan over that scan's own resource labels
  (`"QA"` then `"qa"` staged to the same ZIP name; because every resource
  for a scan downloads before any of them uploads, the second silently
  overwrote the first before either reached its destination). A pre-existing
  symlink at a label-joined subdirectory (`output_dir/<resource_label>`,
  or a scan's staging directory) could previously defeat containment
  checking, since the check anchored to the already-resolved,
  already-escaped path instead of the caller-trusted directory one level
  up; extraction into a resource directory, a scans directory, and a
  per-scan ZIP now all verify the resolved label directory is still inside
  the resolved caller-supplied root before anything is written, catching
  the symlink case that resolving the label path alone could not.
  `SessionService.list`'s `subject` filter, given without `project`, used
  to silently drop the subject filter and issue a site-wide query instead
  of raising -- XNAT subject labels aren't unique without a project to
  scope them -- and now raises. A further sweep of truthiness checks over
  caller parameters across the service layer turned up and fixed five more
  cases where an explicit empty string was silently treated as "not
  provided" and widened scope instead of raising: `SessionService`'s
  modality filter, `DownloadService`'s zip-experiment-ref project lookup,
  `AdminService.get_site_config`'s key filter, `PipelineService.list_jobs`'s
  status filter, and `PrearchiveService.archive`'s subject/experiment-label
  handling (three related sites: the subject lookup used to skip on an
  empty experiment label, an empty subject used to fall through to XNAT's
  DICOM-derived subject instead of raising, and an empty experiment label
  used to be silently omitted from the destination path once a subject was
  resolved). The same sweep also found six sites treating `limit=0` as "no
  limit" instead of "zero results", across `AdminService`, `ProjectService`,
  `SubjectService`, and `SessionService`; all six now check `is not None`.
  A caller-supplied `zip_filename` containing subdirectories (`"sub/dir.zip"`)
  now validates each path component individually instead of only checking
  overall containment, so a component that is itself unsafe (a Windows
  drive letter, a reserved device name, an empty segment) is rejected even
  when the joined path happens to stay inside the output directory; an
  explicit empty string now raises instead of silently falling back to the
  default filename. Finally, `validate_local_path_component` now rejects
  C0 control characters and NUL, matching the check the URL-path label
  validators already had.

  A final round closed five narrow residuals of the same two classes. The
  `admin refresh-catalogs` CLI command's own `--limit` handling (a separate
  code path from `AdminService.refresh_catalogs`'s library-level limit,
  already fixed) still treated `--limit 0` as "no limit" and processed
  every experiment; it now checks `is not None`, so `--limit 0` correctly
  refreshes zero. ZIP extraction (`_extract_scan_zip`) used to validate and
  casefold-register a resource label BEFORE checking whether that resource
  was excluded, so an explicitly-excluded resource with a locally-unsafe
  label (or a case-variant of another label) could still fail the whole
  extraction even though nothing from it was ever written; exclusion is now
  checked first, and only labels that will actually be extracted are
  validated or registered. The same function's `resource_label` override
  used to fall through to the per-member detected label (or `"UNKNOWN"`)
  on an explicit empty string; it now raises (`None` still means "no
  override"). `SessionService.list_sessions`'s `modality` filter -- the
  classified-rows path used by `session list`, distinct from the
  `list_project_experiment_rows`/`list()` filter fixed earlier -- still
  silently widened to every session on an explicit empty string; it now
  raises the same way. The CLI's `--name` flag on `session download`/`scan
  download` checked truthiness before validating, so an explicit `--name
  ""` skipped validation entirely and silently fell back to the session ID
  instead of being rejected; both now validate on `is not None`, so an
  empty value fails the way a value with a path separator already did.
  Finally, `validate_local_path_component`'s Windows-reserved-device-name
  check gained the superscript-digit forms `COM¹`/`COM²`/`COM³` and
  `LPT¹`/`LPT²`/`LPT³` (U+00B9/U+00B2/U+00B3), which Windows reserves the
  same way as the plain-digit `COM1`-`COM3`/`LPT1`-`LPT3` but which the
  existing digit-range check did not match.

  `validate_local_path_component`'s docstring also now names, explicitly,
  the asymmetry between it and `validate_xnat_resource_label`: a resource
  label may legally contain `#`, `?`, or `%` on XNAT and be fetchable over
  HTTP, but `?` (and the other Windows-reserved characters) still fail
  local-path validation -- URL-legal does not imply locally-writable, and
  that gap is accepted design, not a bug to reconcile later.

  Three closing residuals: `scan download`, like `session download`
  already did, now validates the raw session ID as a local directory name
  when `--name` is omitted -- a session labelled `"CON"` is a legal XNAT
  identifier but a reserved Windows device name, and previously reached the
  filesystem unvalidated in that fallback path.
  `SessionService.list_sessions`'s empty-modality guard now runs before
  fetching experiment rows rather than after, so a bad `modality` value no
  longer costs an HTTP request it will just discard.

  `session download`/`scan download`'s `--name` validation now runs in an
  eager Click option callback, at argument-parsing time, instead of inside
  the command body -- so an invalid `--name` is rejected before
  authentication is even attempted, not just before the download starts.

- Locked dependency versions bumped to close two published CVEs with no
  code changes required: `cryptography` 49.0.0 -> 50.0.0 (PYSEC-2026-3552)
  and `pydicom` 3.0.1 -> 3.0.2 (PYSEC-2026-2266). Both stay within their
  existing `pyproject.toml` version ranges.

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
  bind to, and for how long), and a Performance page (measured throughput and
  peak RSS for the transfer paths).

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
