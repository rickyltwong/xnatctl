# Changelog

All notable changes to this project will be documented in this file.

## 0.5.0 - 2026-08-27

**Breaking**

- `project transfer` config: the `filtering` sections `project_resources`,
  `subject_resources`, `subject_assessors`, and the per-xsi-type
  `session_assessors` key are removed and now rejected with an error naming
  the offending section. They never did anything: the transfer pipeline
  moves experiments, scans, scan resources, and session resources only, and
  those sections were accepted in silence while the corresponding data was
  never transferred. If your config sets them, delete the sections -- the
  transferred data is unchanged. `project transfer-init` no longer emits
  them.
- The ten flags deprecated in 0.3.0 and scheduled for removal in 0.5.0 are
  removed, as their warnings promised: `--unzip`/`--no-unzip` (use
  `--extract`/`--no-extract`), `--cleanup` (no replacement; cleanup is
  implicit with `--extract`), `--no-cleanup` (use `--extract --keep-zips`),
  `--include-resources` (use `--session-resources`), `--no-parallel` (use
  `--workers 1`), `--parallel` (no replacement; parallel is the default),
  `--session` (use `--experiment`/`-E`), `--gradual` (use `--mode gradual`),
  and `--archive-format` (use `--mode`). The flags deprecated in 0.5.0
  (`-e`, `--file`, the two command-local `-f` uses, `-s`) still work and
  remain scheduled for removal in 0.7.0.
- Library only, CLI output is unchanged: `XNATClient.ping()` now returns a
  typed `ServerInfo` model and `XNATClient.whoami()` a typed `UserInfo`
  model instead of plain dicts, and `SessionService.get_scans()` /
  `get_resources()` return `list[Scan]` / `list[Resource]` instead of raw
  row dicts. Migration: read attributes instead of keys
  (`info["username"]` -> `info.username`). For `ServerInfo`/`UserInfo`,
  `.model_dump(by_alias=True)` approximates the old dict's wire keys if one
  is still needed; it is not exact for every caller (`by_alias=True` spells
  the name fields `firstName`/`lastName`, not the historical `firstname`/
  `lastname` whoami keys -- use plain `.model_dump()` for those instead).
  For `Scan`/`Resource` rows, there is no dict-shaped equivalent to fall
  back on: `.model_dump()` uses the model's own field names and defaults
  (`id` not `ID`, plus fields like `session_id`/`project` the raw rows never
  carried), so existing consumers of the old row dicts must move to
  attribute access.
- `XNATClient.paginate()` (and the `BaseService._paginate()` wrapper around
  it) are removed. Dead code: no command ever called it, it had no test
  coverage of its own callers, and it is a known infinite-loop hazard
  against any endpoint that ignores `offset`. Use
  `list()` on the relevant service instead -- it fetches the complete
  result set in one call, optionally bounded with `limit=N`, rather than
  paging through it.
- CLI argument conventions reconciled: `pipeline run`/`pipeline jobs`/`admin
  refresh-catalogs` now take `-E` for `--experiment` (was `-e`, the only
  outliers against the `-E` convention used at 14 other sites);
  `resource download` now takes `--output-file` for the output path (was
  `--file` -- the same concept `project transfer-init --output-file`
  already used under a different name; `-f` is unchanged); `api
  post`/`api put --file` and `container logs --follow` lose their `-f`
  short flag (long form only -- `-f` meant two different things across
  those commands and `resource download`, so it now belongs to
  `--output-file` alone); `scan delete`/`scan download --scans` loses its
  `-s` short flag (long form only -- it collided with `-S`/`--subject` on
  the same command line). Also additive, not breaking: `--scans` is now
  repeatable (`--scans 1 --scans 2`), alongside its existing comma-list
  syntax (`--scans 1,2,3`). Every old spelling still works and prints a
  deprecation warning to stderr; removed in 0.7.0. See
  `docs/stability.rst` for the full table.

**Features**

- `session normalize-labels` -- rename a project's experiment labels to the
  standardized `{SUBJECT}_{VISIT:02d}_SE{SESSION:02d}_{MODALITY}` convention,
  with `--dry-run`/`--yes` confirmation. Replaces the experiment-label pass
  of the retired `scripts/apply_label_fixes.py` maintenance script; its
  subject-rename pass is unchanged, use `subject rename` for that.
- New `ServerInfo` and `UserInfo` models (exported from the package root)
  typing the `ping()`/`whoami()` results; `UserInfo` validates the raw
  `/xapi/users/{username}` payload and round-trips its wire keys via
  `model_dump(by_alias=True)`.
- `-o tsv` output format plus a `--no-headers` global flag: plain
  tab-separated lines (header line of column keys, one record per line,
  embedded tabs/newlines sanitized to spaces, stray control bytes and ANSI
  escapes stripped, never colored) for `awk`/`cut` pipelines. Every command
  honors it, including `health ping`, `auth status`, `config show`, and
  `session show`. `--no-headers` also drops the header row from
  `-o table`. Pass `--columns` to pin an exact column set for scripts.
- `--no-color` global flag; `NO_COLOR` and `CLICOLOR=0` are honored
  everywhere, and no status output conveys state by color alone.
- Downloads report ZIP entries skipped as unsafe (path traversal,
  symlink-typed members) instead of silently dropping them: one warning
  naming them, plus an additive `skipped_unsafe_entries` count in the JSON
  transfer summary.
- The config file and session cache are versioned: `config.yaml` gains
  `version: 1`, unknown keys warn instead of being silently ignored, a file
  written by a newer xnatctl is refused by mutating commands rather than
  silently downgraded (`config init --force` is the one documented
  override), and a stale or unrecognized session cache discards itself and
  re-authenticates.
- `--log-file PATH` (also `XNATCTL_LOG_FILE`, or `log_file:` in the config)
  writes a persistent, redacted JSON-lines diagnostics file for the
  invocation: the full DEBUG stream including the per-request HTTP trace
  and the command's final error, independent of `--quiet`/`--verbose`, with
  a per-invocation correlation id, 0600 permissions, and 10 MB rotation.
- DICOM C-STORE TLS is now proven against a real TLS peer in the
  integration tier, and a static guard keeps DICOM tag values out of logs
  and error details package-wide. SECURITY.md documents what xnatctl writes
  to disk and the PHI-in-logs posture.
- `import xnatctl` no longer loads Rich, Click, or httpx -- cold import is
  roughly 5x faster for library consumers. A missing dependency now
  surfaces at first attribute use rather than at import time.

- `project list`, `subject list`, `session list`, `scan list`,
  `resource list`, `prearchive list`, `pipeline list`, and
  `admin user list` gain uniform, CLIENT-side list controls: `--filter
  'field:glob'`, `--sort-by FIELD[:desc]`, `--limit N`, and `--columns
  a,b,c` (table output only -- `-o json` always carries every field).
  `pipeline jobs` and `admin audit` gain `--filter`/`--sort-by`/`--columns`
  the same way, but keep their existing `--limit`, which is still
  forwarded to the server as a request parameter -- UNLESS `--filter` or
  `--sort-by` is also given, in which case the server-side limit is
  dropped for that request (so filtering/sorting sees the full result set
  it needs to work over, not just whatever fit in the small default
  window) and `--limit` is applied client-side afterward instead.
  `xsync list` gains the same filter/sort/limit/columns controls over its
  dynamic, per-deployment row shape.
- `session list --modality` accepts any free-text modality string
  (case-insensitive), not just `MR|PET|CT|EEG` -- XNAT sessions can carry
  arbitrary modality values (`US`, `XA`, `CR`, `MG`, ...), and the filter
  now matches any of them (it used to silently pass every session through
  for a modality outside the old fixed four). `PETMR` is its own value,
  distinct from `PET`. `OCT` is accepted as an alias for `OPT`, the xsiType
  segment XNAT actually archives Optical Coherence Tomography sessions
  under (`xnat:optSessionData`) -- the same xsiType the `scan list` fix in
  0.2.11 already had to work around.
- `ProjectService.list()`, `SubjectService.list()`, and
  `SessionService.list()` (the typed library methods -- no CLI command
  calls them; the list commands above use separate row-listing methods)
  forward `limit` to the server as a request parameter instead of always
  fetching the full result set and slicing it in Python; the client-side
  slice stays in place as belt-and-braces for endpoints that ignore it.
- A one-line update-availability notice, like `gh`/`npm`: after a successful
  command, if a newer xnatctl release exists, a stderr line reports it
  (skipped for `-q`, `-o json`, non-interactive/non-tty output, pre-release
  versions, and when `NO_UPDATE_NOTIFIER`, `XNAT_NO_UPDATE_CHECK`, or `CI` is
  set). The check itself only ever reads a local 24h cache, so it never adds
  latency to a command; refreshing that cache from PyPI happens in a
  detached background process (not a thread, so it isn't killed by the
  parent exiting before the request completes) with a 1-second timeout.
- `xnatctl upgrade` updates xnatctl in place: it detects how the install was
  made (standalone PyInstaller binary, pipx, pip/uv virtual environment, or
  Docker) and either runs the matching package-manager command or, for a
  standalone binary, downloads and verifies the latest GitHub release asset,
  runs it with `--version` as a sanity check, and only then atomically
  replaces itself -- running `--version` again against the installed copy
  and automatically rolling back to a kept backup if that second check
  fails. On Windows, where a running `.exe` can't be overwritten or deleted,
  the previous binary is renamed aside instead and cleaned up on the next
  launch. Dry by default -- pass `--yes`/`-y` to actually run it; a Docker
  install only ever prints the `docker pull` command, never subprocesses it.
  `xnatctl upgrade --check` does an on-demand PyPI lookup (bypassing the
  notice's 24h cache) and reports up-to-date/newer-available/unreachable.
  `update_check: false` in config.yaml opts out of both the notice and this
  command's background cache refresh, and the notice now points at
  `xnatctl upgrade`. Both the notice and the background refresh now require
  a loaded config to run at all -- a command that never loads one (shell
  completion, `local extract`, `config init`, ...) neither notifies nor
  spawns a PyPI fetch behind the scenes.
- `subject delete` and `scan delete` gain `--batch FILE`: a file of IDs (one
  per line, or a JSON array of strings), or `--batch -` to read the list
  from stdin -- e.g. `xnatctl subject list -P PROJ -q | xnatctl subject
  delete -P PROJ --batch - --yes`. `--batch` is mutually exclusive with the
  positional `SUBJECT_ID` / `--scans`; `--batch -` requires `--yes` or
  `--dry-run` since the confirmation prompt would otherwise read from the
  same stdin the batch list is consuming.
- Direct-to-archive REST uploads (`session upload --direct-archive`,
  `upload-exam`) are checked against the server's XNAT version before any
  files move, and fail with an actionable "requires XNAT >= 1.8.3; server
  reports X.Y.Z" error instead of an opaque server-side failure partway
  through. The version is probed once per client, on its own short timeout
  independent of the command's transfer timeout, and cached; when it can't
  be determined, the upload proceeds unblocked rather than guessing.
  `upload-dicom` is a separate DICOM C-STORE transport and is not affected
  by this check. See `docs/xnat-compatibility.rst`.
- New Container Service surface, for servers with the plugin installed:
  `command list/show/create/update/delete`, `wrapper list`, `wrapper
  enable/disable` and `wrapper config get/set`, `container
  list/show/logs`, and `admin docker images/hubs/server/pull`. Launching
  and killing containers land later. Two server behaviours are worth
  knowing before using the mutating verbs: `command update` is a full
  replace, so a payload without its `xnat` block removes every wrapper on
  that command (`--dry-run` shows the diff, including the removal); and
  XNAT answers a delete of a nonexistent command id with success.
  `wrapper list` is derived
  client-side from each command's embedded wrappers because the plugin
  exposes no wrapper-listing endpoint of its own. `container list` prefers
  a project scope (`-P`, or the profile's `default_project`) over the
  site-wide listing. Every endpoint path was checked against a running
  server with Container Service 3.7.2 rather than inferred, and the
  integration tier exercises the command/wrapper ones. The container
  object's field names come from the plugin's own annotations rather than
  an observed response -- populating one needs a reachable Docker daemon,
  which the test stack deliberately does not provide -- so `container
  list/show` is the one part of this not exercised end to end.
- Cross-project sharing: `subject share/unshare` and `session
  share/unshare` put a subject or session into a second project without
  moving it, optionally under a different label there (`--label`).
  `subject show` and `session show` list the projects a resource is shared
  into (a `shared_projects` field under `-o json`). Unsharing a
  resource from its *primary* project is refused: XNAT answers that by
  deleting the resource and everything under it, with a response
  indistinguishable from removing an ordinary share.
- Custom variables: `subject vars`, `session vars` and their `vars set`
  forms read and write the project-defined fields XNAT calls custom
  variables (the ground `xnat-varput` covers). Several `KEY=VALUE` pairs in
  one `vars set` are written in a single request.
- New, Provisional (not in `xnatctl.__all__`): `AsyncXNATClient`, an
  asyncio client over `httpx.AsyncClient` for the READ path -- `get`,
  `get_json`, `stream`, and `from_profile`. It shares the sync client's
  retry policy and typed exceptions, so `except SessionExpiredError` works
  the same either way. Uploads, downloads, and service accessors stay
  synchronous. Import it from `xnatctl.core.async_client`; being outside
  `__all__`, it may change between minor releases.

- `container launch`, `container kill`, and `--follow` on `container logs`
  complete the Container Service group. `launch` takes a wrapper by id or
  name and `--param KEY=VALUE` inputs, with `--wait` polling to a terminal
  status. It resolves the wrapper locally first, and that is not a nicety:
  the server answers a launch against a wrapper id that does not exist with
  HTTP 200 `"success"` and then creates nothing, so an unresolved typo
  would report success and silently do no work.
- New `anon` group for DICOM anonymization scripts: `anon show/set`, and
  `anon enable/disable`, each addressing either the site-wide script or one
  project's override via `-P`. `anon set --dry-run` prints a unified diff
  against the script currently on the server, and the confirmation names
  the scope being replaced -- these scripts are what stands between PHI and
  the archive, so replacing the site's while meaning a project's should not
  be an easy mistake to make.
- New `scp` group for DICOM SCP receivers: `scp list/show/create/delete`
  and `scp enable/disable`. AE titles and ports are validated before any
  request is sent.

- New `event` group for Event Service subscriptions: `event
  list/show/create/delete`, `event enable/disable`, plus `event actions`
  and `event types` listing the valid action keys and trigger types to
  build a subscription from. Note the server's own route spelling is
  asymmetric -- listing is `/xapi/events/subscriptions`, everything else
  `/xapi/events/subscription` -- which the service handles.
- New `search` group for saved searches: `search list/show/run/delete`.
  `search run` renders the result set through the normal output path, so
  its per-search column shape works with `-o json|table|tsv` and `-q`.
- `prearchive settings [-P PROJECT]` shows a project's routing mode, and
  `--set manual|auto-archive|auto-archive-overwrite` changes it. Modes are
  named rather than numeric, and are validated before the request goes out:
  XNAT stores any integer you send it -- 3 and 9 are accepted and read back
  unchanged -- so a typo would otherwise leave a project routing to nothing
  meaningful while reporting success. A project already carrying such a
  value reads back as `unrecognized (N)` rather than being guessed at.

**Fixes**

- `subject vars set` no longer creates the subject it was asked to update.
  Its PUT is the same create-or-update route `subject create` uses, so a
  mistyped `--subject` silently wrote a new empty subject into the project
  and reported success (verified against XNAT 1.9.2.1: the PUT answers 201
  for a subject that does not exist, never 404). It now confirms the subject
  exists first, under `--dry-run` as well.

- Mixing a deprecated flag with its current spelling on one command line no
  longer drops values. `admin refresh-catalogs -E NEW1 -E NEW2 -e OLD1`
  reached the command as `('OLD1',)` -- both `-E` values discarded, exit 0,
  those experiments simply never refreshed. Repeatable options now merge
  both spellings, so nothing is lost whichever order they are typed in.
  Affects `-e` on `admin refresh-catalogs` and `-s` on `scan
  delete`/`scan download`. Single-valued options are unchanged: only one
  value can survive, and the deprecated spelling still takes precedence
  there.

- `container launch --wait` no longer times out on a launch that succeeded.
  It snapshotted the existing containers *after* firing the launch, so the
  new container was already in the "already existed" set and never matched
  -- and because the launch response usually carries the placeholder
  `workflow-id: "To be assigned"`, the id-matching path could not fire
  either. It now snapshots before launching.

- `container launch` fails instead of reporting success when the server
  returns a failure launch report; `container launch --wait --timeout` and
  `container logs --follow --interval` reject non-positive values at parse
  time rather than after queueing a container.

- `wrapper config set --dry-run` works for a wrapper that has no
  configuration yet. Dry-run read the current config to build a diff, which
  the server answers with a 404 in that case, so the preview failed while
  the same command with `--yes` succeeded. It now previews a creation.

- `scp create` rejects a port another receiver already uses, as its
  documentation always claimed it did. Two receivers cannot both bind one
  socket, and XNAT accepts the duplicate silently.

- `search delete` fails on a saved search that does not exist, instead of
  reporting that it deleted one. Saved-search listing, `search run`, and
  `session normalize-labels` now raise on a malformed response body rather
  than reporting an empty result set -- `normalize-labels` in particular
  reported "renamed 0" when the server had not answered the question asked.

- `session list --modality` no longer classifies an xsiType with a trailing
  newline as a valid modality.

- `prearchive settings --set manual` reports a permission failure as a
  permission failure. Every 403 was rewritten as "this is site policy", an
  explanation that only applies to the auto-archive modes.

- The update-availability check now works on the standalone binary. Its
  background refresh re-invoked `sys.executable -m ...`, which on a
  PyInstaller build is the `xnatctl` binary itself rather than a Python
  interpreter, so the child exited immediately, the cache never refreshed,
  and the "newer release available" notice never appeared -- for exactly
  the users who cannot `pip install -U`.

- `session upload --session` and `session upload-exam --session` work again.
  `--session` is the deprecated spelling of `--experiment`, documented as
  working until it is removed, but passing it alone failed with
  `Missing option '--experiment' / '-E'` -- Click enforces an option's own
  required-ness regardless of another option's callback having already
  supplied the value, so the alias forwarded, warned, and was then rejected.
  The exact pre-deprecation invocation the alias exists to keep working was
  the one that did not.

- `subject delete`'s pre-delete safety check (refusing to delete a subject
  that still has attached experiments) no longer fails open: a network or
  auth error while checking now aborts the delete instead of being treated
  as "no experiments attached" and letting the delete -- and XNAT's
  cascade-delete of any real experiments -- proceed anyway. Only a
  genuinely-gone subject/project is still read as "no sessions."
- `resource show`, `scan show`, `session show`, and `subject show` no
  longer render a listing failure as an empty list indistinguishable from
  "genuinely empty" -- a warning is now printed to stderr when the
  files/resources/scans/sessions fetch fails.
- `resource upload`'s "resource may already exist" fallback caught any
  exception, including auth and network failures unrelated to the resource
  already existing; it now narrows to the actual 409 response.
- The transfer-state database (`~/.config/xnatctl/transfer.db`) is created
  owner-only (0600) instead of inheriting the process umask, and a rotated
  `audit.log.1` is tightened to 0600 instead of keeping a legacy file's
  looser mode.
- The CLI reference now documents the whole `xsync` family, `resource
  refresh`, and `dicom modify` (a drift test keeps it in sync with the real
  command tree), and the piped `xsync refresh-credentials` examples show
  `--yes` -- without it the confirmation prompt consumed the piped
  password.

## 0.4.0 - 2026-08-22

**Breaking**

- `DownloadService.download_session` is removed. It was never wired to any
  command; use `download_session_fast`, `download_scans`, or
  `download_resource`.
- Three library exceptions no longer shadow stdlib names:
  `ConnectionError` -> `XNATConnectionError`, `TimeoutError` ->
  `RequestTimeoutError`, `ValidationError` -> `InputValidationError`. The old
  names survive as deprecated aliases that warn on instantiation (removed in
  a later minor release); update `except` clauses to the new names.
- Single-target download/upload service methods raise typed exceptions
  instead of returning a failure summary: `download_resource`,
  `upload_resource`, and `download_scan` (when given a resource label) return
  their summary only on success. Batch methods still return summaries and
  gain `raise_for_status()`. The CLI's exit codes and messages are unchanged.
- `DownloadService.download_session_level_resources` returns the downloaded
  `(label, path)` pairs instead of an `int` count; use `len(...)`.
- `xnatctl.services.uploads` is gone; `UploadService` and its helpers now
  live in `xnatctl.services.upload`, split one module per transport. The
  top-level `xnatctl.UploadService` import is unaffected.
- `-o json` changes shape on the five commands that already shipped
  structured JSON: `scan download`, `session upload` (single-archive and
  directory-batch), `session upload-dicom`, and `session upload --mode
  gradual`. Each now prints one shared `TransferSummary` object --
  `operation`, `session_id`, `project`, `output_dir`/`source`, `scans`,
  `files`, `bytes`, `duration_seconds`, `status`
  (`"success"`/`"partial"`/`"failed"`, always agreeing with the exit code),
  per-item `items`, and a nested `verification` report with `--verify`.
  `files`/`bytes` only ever count what actually transferred (`null` where
  the transport doesn't track that). No compatibility shim: scripts reading
  the old top-level keys (`success`, `output_path`, `total_size_mb`,
  `total_files`, `sent`, `errors`, ...) must update. Full schema in the
  Downloading/Uploading docs pages; `session upload-exam`'s separately
  documented `-o json` contract is unaffected.

**Features**

- **User lifecycle administration.** `admin user` gains `list`, `show`,
  `enable`/`disable`, `roles`, `groups`, `remove`, and `kill-sessions` --
  the fix-command for a shared account that has exhausted its
  concurrent-session limit and started failing every new login with 401s.
- **Project access and membership.** `project` gains `users`,
  `grant`/`revoke`, `access` (get, or `--set public|protected|private`), and
  read-only `requests`. Approving/denying a request is deliberately not
  offered: XNAT resolves it against whichever account is signed in, so an
  admin cannot safely act on another user's behalf.
- **Site config, plugins, and server info.** `admin site-config get [KEY]` /
  `set KEY VALUE`, `admin plugins` (`plugins show ID` for one), and
  `admin version`.
- **`--verify` on `session download` and `scan download`** checks every
  downloaded file's MD5 against the server's catalog checksums -- extracted
  or still zipped, session-level resources included, files matched by scan
  and resource rather than filename alone. A mismatch, a listed file that
  never landed, an ambiguous mapping, or a run where nothing was verifiable
  at all (no checksums on record, or an empty server manifest despite files
  on disk) fails the command; local files the manifest doesn't cover are
  reported rather than silently skipped.
- `session download`, `resource upload`, and `resource download` gain
  structured `-o json` output for the first time, using the shared
  `TransferSummary` shape.
- DICOM utilities and OS-keychain password storage now ship in every
  install, including the standalone binary; the `dicom` and `keyring`
  extras are gone.
- `XNATClient.stream()` is a public API: streamed reads (large file
  downloads) through the client's own retry, auth, and typed-error
  contract, replacing any need to reach for raw httpx access.
- **One-call library connect.** `xnatctl.XNATClient.from_profile("prod")`
  builds a client from a saved profile using the CLI's own credential
  resolution, each resource type is reachable through a cached accessor
  (`client.projects`, `client.sessions`, ...), and the package root
  re-exports the client, config, service classes, models, and the exception
  hierarchy as the supported public surface. The top-level `xnatctl`
  namespace is now **Stable** and semver-covered, superseding its earlier
  Provisional status; submodule internals stay Provisional. README gains a
  library quickstart.

**Fixes**

- 404 classification no longer depends on error-message text. The client
  enriches every 404 with `status_code`/`method`/`path` and services
  dispatch on exception type, so an unrelated error mentioning "404" (a
  session labelled `SUB404`, say) can no longer be misread as "not found".
- Downloads now go through the client's retry, auth, and typed-error path
  instead of talking to httpx directly -- restoring the retry ladder, error
  mapping, and basic-auth fallback (a client built from credentials but not
  yet logged in used to fail every download).
- Downloads are written atomically: bytes stream to a `.part` file renamed
  into place only after the byte count matches the server's
  `Content-Length`, so a dropped connection can no longer leave a truncated
  file that looks whole.
- A corrupt session ZIP now fails the command -- CRC-checked before
  extraction, nonzero exit -- instead of printing a note and exiting 0.
- Cross-server transfer scan imports retry only transient failures, with
  the same transient-vs-permanent HTTP 400 discrimination the upload paths
  use; a permanent error fails on the first attempt instead of burning the
  full backoff ladder.
- **The service layer now validates and encodes its own REST paths** instead
  of trusting the CLI to have done it. Every caller-supplied identifier is
  percent-encoded where it enters a URL, and the hierarchy refs reject
  separators, dot-only segments, control characters, and leading/trailing
  whitespace at construction -- a library caller can no longer redirect a
  request with an ID like `SUB1/experiments/XNAT_E1?activate=` or `..`, and
  an empty segment raises instead of silently building a collection route
  (`delete("")` used to become `DELETE /data/projects/`). IDs made of
  alphanumerics/dots/dashes/underscores produce byte-identical URLs.
- **An explicit empty string no longer widens scope.** Across the service
  layer and CLI, a passed-in `""` used to be conflated with "not provided"
  and silently broadened the operation -- most dangerously
  `ResourceService.delete(scan_id="")` targeting the session-level resource,
  and empty project/subject/modality/status filters issuing site-wide
  queries. All such sites now raise, and six places treating `limit=0` as
  "no limit" now return zero results.
- **Server-reported names are validated before touching the local
  filesystem.** A scan ID or resource label used as a local file or
  directory name -- in downloads, ZIP extraction, and transfer staging --
  must be a safe path component on POSIX, Windows, and macOS: separators,
  drive-qualified values (`C:escape`), reserved device names (`CON`,
  `COM1`, ...), leading/trailing dots or spaces, Windows-invalid characters,
  non-NFC Unicode, and control characters are all rejected. Two sibling
  values differing only by case (`DICOM`/`dicom`) raise instead of silently
  merging on a case-insensitive filesystem, and extraction verifies the
  resolved target is still inside the requested output directory, closing a
  pre-existing-symlink escape. `--name` on `session download`/`scan
  download` runs the same check, at argument-parsing time.
- Locked dependency versions bumped to close two published CVEs, staying
  within their existing ranges: `cryptography` 49.0.0 -> 50.0.0
  (PYSEC-2026-3552) and `pydicom` 3.0.1 -> 3.0.2 (PYSEC-2026-2266).

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
