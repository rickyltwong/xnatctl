# Security Policy

## Supported Versions

Security fixes are provided for the latest released version of `xnatctl` on a
best-effort basis.

If you report a vulnerability against an older release, maintainers may ask you
to reproduce the issue on the latest version first.

## What xnatctl may write to disk

Everything below `~/.config/xnatctl` that is secrets-adjacent is written
0600 (owner read/write only) via `core/fsutil.py`'s atomic-write helpers, not
via a plain `open()` that inherits the process umask. Directories under it
are created 0700 the same way.

- **`~/.config/xnatctl/.session`** -- cached JSESSIONID and its expiry.
  Rewritten atomically on every login/reauth (`core/auth.py`), 0600.
- **`~/.config/xnatctl/config.yaml`** -- profile config. Holds a plaintext
  `password` field for any profile not using `password_source: keyring`.
  Written via the same atomic 0600 path (`core/config.py`); a startup check
  warns (and prints a `chmod 600` hint) if the file is ever found looser than
  that, which catches a copy from elsewhere or a manual edit.
- **`~/.config/xnatctl/audit.log`** -- JSON-lines record of destructive
  operations (`core/logging.py`'s `AuditLogger`). Append-only; each write to
  `audit.log` re-checks and tightens *that* file's mode to 0600 if it has
  drifted looser, so a copy from elsewhere or a manual edit gets caught over
  time. This is not the same guarantee as the atomic 0600 *creation* the
  session cache and `config.yaml` get -- an append-only file cannot be
  swapped into place the way those are rewritten, and a legacy file created
  before this code existed keeps whatever mode it had until its next write.
  Rotates once at 10 MB, renaming the current file to `audit.log.1`;
  rotation happens *before* the tightening check runs, and that check only
  ever looks at the current `audit.log`, never at `audit.log.1` -- so a file
  that was looser than 0600 at the moment it crossed the size threshold
  becomes a permanently-loose `audit.log.1`, with nothing in `xnatctl` ever
  revisiting it afterward. Known gap, not yet fixed; see "PHI in logs and
  errors" below for what the log records.
- **`~/.config/xnatctl/.update-check`** -- cached "is a newer version
  available" result (`core/update_check.py`). Not a secret, but written 0600
  via the same helper for one code path over everything under this directory.
- **`~/.config/xnatctl/transfer.db`** (plus its `-wal`/`-shm` siblings when
  the database is in WAL mode) -- cross-server sync history and ID mappings
  (`core/state.py`'s `TransferStateStore`), which can hold subject/session
  labels and source/destination server URLs from a completed `xsync` run.
  `sqlite3.connect` creates a missing file with the process umask, not 0600,
  so `TransferStateStore` pre-creates it privately before connecting and
  tightens the `-wal`/`-shm` siblings right after enabling WAL mode (they are
  SQLite's own file creations, not xnatctl's, so the pre-create doesn't cover
  them). A pre-existing looser database is tightened on open the same way
  `AuditLogger` tightens on write.
- **Temp archives during upload staging** -- `session upload`'s REST-batch
  transport builds tar/zip archives of the DICOM files being sent
  (`services/upload/rest_batch.py`, `archives.py`, `exam_upload.py`) in
  `tempfile.mkdtemp()`/`TemporaryDirectory()` locations, which are 0700 by
  Python's own default on POSIX. Always removed in a `finally` block
  regardless of success or failure.
- **DICOM C-STORE workspace** -- `session upload-dicom` stages its
  per-association batch logs the same way (`services/upload/dicom_store.py`,
  `tempfile.mkdtemp(prefix="xnatctl_dicom_store_")`, 0700). Those log files
  record file paths and DICOM status codes, never tag values (see below).
  Removed on full success; deliberately kept on disk when any file failed to
  send, so the batch logs are available to debug the failure -- there is no
  CLI flag to change this, so clean it up by hand if that matters to you.
- **`dicom modify`'s backup and staging files** -- `--backup` writes a
  same-directory `<file>.bak` copy (`cli/dicom_cmd.py`) before modifying,
  preserved permanently -- it is the point of asking for a backup, so nothing
  removes it automatically. The modification itself is staged as a
  same-directory `<file>.dcm.tmp` (`tempfile.mkstemp`, original file's mode
  copied onto it) and `os.replace`d into place; removed if the write fails
  partway through. Both live next to the input file, wherever the caller
  pointed the command, with that file's own permissions -- not under
  `~/.config/xnatctl`, and not xnatctl secrets.
- **Downloaded content and extracted archives** -- written to the
  `--output-dir` the caller specified, with normal (umask-governed)
  permissions, because this is the user's own requested output, not an
  xnatctl secret. `services/downloads.py`'s `stream_to_file` stages each
  download as a same-directory `.part` file (suffixed with the writer's PID
  and thread id) and renames it into place only on success; a failed
  download's partial file is removed rather than left behind.

## PHI in logs and errors

**The rule:** identifiers the user supplied on the command line or that
xnatctl resolved from the XNAT API for the operation they commanded --
project/subject/session IDs and labels, file paths -- may appear in log
lines, retry warnings, and exception `details`. This is a deliberate choice,
not an oversight: the same identifiers are already in the operator's shell
history, and `AuditLogger` (`core/logging.py`) records them for the same
reason -- an audit trail that only starts once the operator remembers to turn
it on is useless.

**DICOM tag *values* read out of files are different and are never logged.**
`PatientName`, `PatientID`, `PatientBirthDate`, `PatientSex`,
`AccessionNumber`, `InstitutionName` and the rest of the identifying tag set
are parsed in exactly one place in the codebase: `cli/dicom_cmd.py` (`dicom
validate/inspect/anonymize/list-tags/modify`), and that data goes to the
command's own stdout -- its declared purpose -- never to a log line, a retry
warning, or an exception's `details`. `tests/test_phi_logging.py` enforces
this statically: it parses every module under `xnatctl/` (by both tag name
and DICOM tag number, e.g. `0x00100010`/`(0x0010, 0x0010)` for `PatientName`)
and fails if one is referenced anywhere outside that one file, and separately
fails if `dicom_cmd.py` itself ever passes one to a logging call -- reading a
tag for the command's own stdout is fine, logging it is not. That guard is
static analysis of the source, not a runtime proof: it cannot catch a tag
name assembled at runtime (e.g. string concatenation), which is a real,
documented gap in the test's own module docstring, not a claim of
completeness. `services/upload/dicom_store.py` (the C-STORE transport) does
parse DICOM files with `pydicom`, but only to populate missing SOP UIDs
before sending -- its batch logs record file paths and DICOM status codes,
never a tag value.

Two things worth knowing about the boundaries of this:

- A `ServerError`/`ClientRequestError`'s `details` may include a short,
  truncated snippet of the HTTP response body (`core/transport.py`'s
  `_body_snippet`), passed through `redact_url_query` for secret-shaped query
  values only. This is a real, unenforced boundary, not a closed one: that
  snippet is server-controlled text, and xnatctl cannot recognize an
  arbitrary patient identifier a misconfigured or unusual XNAT deployment
  chose to echo into an error message the way it recognizes a URL query
  parameter. In ordinary operation XNAT's error responses (auth failures,
  404s, validation errors) do not carry session or patient metadata -- that
  normally only comes back on a successful data response, which does not
  flow through this path -- but "normally" is an operational expectation
  about XNAT's behavior, not something xnatctl enforces. Treat `-v` output
  (and anything in a bug report generated with it) accordingly if the server
  in question is one you don't fully control.
- `-v`/`--verbose` raises xnatctl's own loggers to DEBUG and `httpx` to INFO
  (one line per request: method, redacted URL, status code).
  `XNATCTL_DEBUG=1` additionally enables `httpcore` at DEBUG for a full
  per-socket protocol trace. That trace is verbose but not a body dump: it
  was read against the installed `httpcore` (`_sync/http11.py`) to confirm
  the `receive_response_body` trace event's payload is the `request` object
  only, not the response bytes it streams -- so even XNAT's own
  `dcmPatientName`/`dcmPatientId` experiment fields, if a `session list`/
  `session show` response happened to carry them, do not reach this trace.

**Redaction scope.** `RedactionFilter` + `redact_url_query`
(`core/redact.py`, `core/logging.py`) scrub secret-shaped URL query
parameters (`password`, `token`, `api_key`, and the rest of
`SECRET_QUERY_KEYS`) and the password half of a `user:pass@host` authority,
applied to every log record's formatted message. Two honest limits: it does
not cover exception tracebacks (the CLI's own traceback path in
`cli/common.py` redacts separately; a `Formatter`-level fix is future log-file
work), and it does not redact DICOM/patient content -- because, per the audit
above, none is ever placed in a log message to begin with.

## Session Cache on Windows

`xnatctl` caches the XNAT session token at `~/.config/xnatctl/.session`. On
POSIX systems this file is created with 0600 permissions (owner read/write
only) and rewritten atomically, so it is never briefly world-readable, even
across concurrent runs.

POSIX permission bits do not carry over to Windows: `os.chmod` there only
toggles the read-only attribute, and a file's reported mode does not reflect
its actual access control. On Windows, protection of the session cache (and
of `config.yaml`, for profiles that store a plaintext password) relies
entirely on the user-profile directory's ACLs, not on any mode `xnatctl`
sets. `xnatctl` does not manage Windows ACLs itself.

On a multi-user Windows machine where profile ACLs may be weaker than usual
(shared profiles, inherited permissions disabled), do not rely on the
session cache file's own protection. Prefer:

- `XNAT_TOKEN` to supply a session token per invocation. The environment
  token itself is never written to disk, but a cache file left by earlier
  password logins is still read -- delete `~/.config/xnatctl/.session` (or
  run `xnatctl auth logout`) once when switching to token-only use.
- The OS keychain for the profile password, via `xnatctl config
  set-password`, instead of a plaintext `password` in `config.yaml`.

## Reporting a Vulnerability

Please **do not** open public GitHub issues for security vulnerabilities.

Instead, email the maintainer directly at **rickywonglt15@outlook.com** with:

- A description of the vulnerability and impacted functionality.
- Steps to reproduce or a proof of concept.
- The version of `xnatctl` and your environment details.
- Any suggested remediation, if available.

You will receive an acknowledgment as quickly as possible, typically within
5 business days.

After triage, maintainers will coordinate remediation and public disclosure.
We ask that you provide reasonable time to resolve the issue before sharing
details publicly.
