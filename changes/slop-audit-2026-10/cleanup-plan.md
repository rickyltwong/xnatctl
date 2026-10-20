# xnatctl slop audit: ranked cleanup plan

Audit of `rickyltwong/xnatctl` at `a99e877` (v0.5.1, `main`). Read-only. No file in the repository was changed.

**Revision 2.** An independent verifier session checked this plan, and I reviewed the `dev` branch (`f017cf7`, 7 commits ahead of `main`). Later, `dev` and `main` moved to 0.6.0 (`67c4931`); that commit changes only the version number, so the plan still applies. I checked every correction below in the code before I applied it. The changes are listed in "Revision notes" at the end of this section. Base the cleanup PRs on `dev`.

## Method and coverage

1. **Layout swarm.** 21 fresh headless sub-agent sessions (high reasoning effort, tools Read and Glob only) read all 292 Python files under `xnatctl/`, `tests/` and `scripts/` in full. (`docs/conf.py` and `docker/integration/tls-scp/scp.py` were out of scope.) They recorded each symbol, its outbound references, and the tests that touch it. A merge step built per-scope inbound-reference hints from that data.
2. **Scoped audit.** The code was split into 95 scopes: 55 production scopes of about 1,000 lines and 40 test scopes of about 1,600 lines. Each scope had a fresh finder session and then a separate fresh adversarial reviewer session. Both had Read and Glob only, with no text search. 24 slots ran in parallel through a queue. Total: 211 sessions, no failures, about $190.
3. **Coverage check.** For all 95 scopes, both the finder and the reviewer reported that they read every scope file in full. Their line counts match the real files exactly. No file was left unassigned.
4. **Lead audit.** I checked every surviving item: 379 survivors out of 393 items (347 finder findings plus 46 reviewer additions, minus 14 refutations). For each production finding and each high-value test finding, I read the cited code myself and traced the callers. For low-stakes test duplicates, I used the two agents' independent traces and spot-checked them. The appendix marks which items I read myself ("Lead-read" or "lead-verified").
5. **Zero findings accepted.** Three scopes (P08, P16, P42) are clean. I reviewed their rejection lists.

Verdict totals after revision 2: 300 accepted, 29 duplicates across scopes, 24 merged into another finding, 24 rejected, 4 maintainer decisions, 20 doc and comment notes.

### Revision notes

| Change | Source | Checked by lead |
|---|---|---|
| New Tier 0 row 11: the 409 tolerance in `scan_transfer.py:619-626` is dead, so a 409 fails the scan. | Verifier session | Yes: `create_scan` calls `self.dest.put`, which raises typed `ClientRequestError`, never `httpx.HTTPStatusError`. |
| New Tier 1 row 26: the dead `except httpx.HTTPStatusError` branch in `executor_archive.py:300-309`. | Verifier session | Yes |
| Tier 2a diagnosis rewritten. The guards run first; the defect is that the assertions still pass if the guard is deleted. Exit codes corrected to 1, 2 and 5. T09-R1 reduced to "strengthen only". | Verifier session | Yes, for the decorator order in `cli/common/__init__.py:621-638` |
| P40-F1 removed: on `dev`, `--subject-pattern` now uses the parameter. | Dev review | Yes |
| T12-F5 removed: its audit assertion is the only discriminating check. | Verifier session | Accepted |
| P48-F3, P36-F2, P49-F5, P50-F2, P28-F1, P41-F1, P44-F1, T21-F5, P32-F1: wording, scope or estimate corrected. | Verifier session | Yes, for P36-F2 (`hierarchy.py:539` vs `553`) and P28-F1 |
| PR sequence: P48-F3 added; P50-F2 moved to PR 3; T10-F1 before T01-F1; docs rebuild and CHANGELOG for Provisional removals. | Verifier session | Yes |
| Two dev-only test items (D-1, D-2) and one docs-drift note (V-3). | Dev review, verifier | Yes |

Line estimates come from the agents, after the reviewers corrected them. Treat them as approximate.

---

## Tier 0: defects found during the audit (fix these first)

These are not slop to delete. They are behaviour bugs or false safety claims that the dead-code trace exposed.

| # | ID | Problem | Location | Action |
|---|---|---|---|---|
| 1 | P12-F1 | `project transfer --workers/-w` is a documented Stable flag. The value is resolved and then never read, so the flag has no effect. | `xnatctl/cli/project.py:446` | Wire the value into `TransferConfig.scan_workers`. If you do not want the flag, deprecate it through `DEPRECATED_FLAGS`. Do not just delete it. |
| 2 | T07-F2 | Two tests can write the developer's real `~/.config/xnatctl/config.yaml`. The `HOME` override does nothing, because `CONFIG_FILE` is fixed at import time. The tests also assert only `exit_code in (0, 1)`. | `tests/test_cli.py:130-149`, `xnatctl/core/config.py:37-38` | Delete the `TestConfigCommands` class. |
| 3 | P28-F1 | `RetryBudget` is never constructed. The docstring of `upload_with_retry` promises a bound on unrecognised permanent 400s, and that bound does not exist at runtime. | `xnatctl/core/retry.py:210-239, 402, 472, 500` | Decide: pass a budget from the upload callers (`rest_archive.py:145,159`, `gradual_client.py:179`, `upload/resources.py:154`), or delete the class, the parameter and its tests (about 125 lines). Either way, fix the `is_permanent_400` docstring (`retry.py:187-188`), which also claims the budget exists. |
| 4 | P54-F4, P17-F1, P55-F1 | `archive_workers` does nothing. The docstring ("Parallel archive workers") is false. The parameter sits on the Stable `UploadService.upload_dicom_parallel` path. | `xnatctl/services/upload/rest_batch.py:203,229`; CLI `session_upload_rest.py:249,452`; `scripts/bench_transfer.py:338` | Deprecate the service parameter and fix the docstring. Remove the CLI and bench plumbing. |
| 5 | P14-F1 | `scan download --keep-zips -r X` prints the path of a file that does not exist (`scans_X.zip`). The real file is `scans.zip`. The branch for more than one resource is unreachable. | `xnatctl/cli/scan_download.py:381-388` | Collapse to one message that names `scans.zip`. |
| 6 | P38-F2 | `".tar.gz"` can never equal `Path.suffix`, so a `.tar.gz` upload never gets the tar content type. | `xnatctl/services/resources.py:411` | Use `name.endswith(".tar.gz")`. |
| 7 | P19-F1 | A release archive without the binary raises a raw `KeyError` ("Unexpected error"). The friendly `UpgradeError` never shows. | `xnatctl/cli/upgrade.py:318-336` | Catch `KeyError` around `extract()` and raise `UpgradeError`. |
| 8 | P30-F5 | `validate_timeout` and `validate_workers` exist and are tested, but nothing calls them. `--timeout` and `--workers` reach the client unvalidated. | `xnatctl/core/validation/network.py:136-237` | Decide: wire them in, or delete them (about 150 lines with tests). |
| 9 | P32-F1, P37-F1/F2, T26-R1 | `AdminService.get_user`, `PipelineService.get` and `PipelineService.status` return any dict as-is before extraction. A `ResultSet` envelope is a dict, so the extraction branch is unreachable. It is also unreachable for a non-dict, because `_extract_results` returns `[]` for any non-dict. Tests pin the current return shape. | `services/admin.py:267-271`, `services/pipelines.py:74-79, 142-147` | Maintainer question: is the raw envelope the intended return value? If yes, delete the dead branch. If no, extract first. The return shape is a Stable surface. |
| 10 | P16-S1 | A user-facing error names `--dicom-host`, but the option is `--host`. | `xnatctl/cli/session_upload_dicom.py:67` | Fix the message. |
| 11 | V-1 | A scan-shell PUT is meant to tolerate "already exists". It catches `httpx.HTTPStatusError`, which `XNATClient` never raises; a 409 arrives as `ClientRequestError`. So a 409 fails the scan transfer. | `xnatctl/services/transfer/scan_transfer.py:619-626` | Catch `ClientRequestError` with `status_code == 409`. Add a regression test. |

---

## Tier 1: high-value production cleanup

Ordered by value: lines removed, drift risk removed, and confidence.

| # | IDs | What | Where | ~Lines | Notes |
|---|---|---|---|---|---|
| 1 | P42-D1, P18-F1, P18-F2 | **Two subject-rename engines.** `SubjectService.rename_batch` and `rename_pattern` differ from the CLI: they use a prefix match, do no target validation, have no `{project}` placeholder, and "merge" by plain rename. The CLI repeats its own execute step three times. | `services/subjects.py:248-430`; `cli/subject.py:535-688` | 180 + 65 | Move the CLI engine into `SubjectService` and make all callers use it. The class is Stable, so keep the method names. Needs a maintainer decision on the semantics. |
| 2 | P26-F1, P26-F2 | `LogContext`, `log_context` and `get_logger` are dead. They are reachable only through `xnatctl.core.__all__` (Provisional). | `core/logging.py:294-303, 532-626`; `core/__init__.py:22,159,162` | 110 | Add a CHANGELOG note. |
| 3 | P30-F1 to F4 | Unused validators: `validate_archive_path` with `ALLOWED_ARCHIVE_EXTENSIONS`, `validate_dicom_directory`, `validate_url_or_none` and `validate_project_list`. | `core/validation/paths.py:27, 414-450`; `network.py:90-94`; `__init__.py:182-204` | 105 | Provisional. The tests for them go too. |
| 4 | P47-F2 | `TransferExecutor.transfer_scan_dicom` is a wrapper with no caller. The pipeline calls download and upload separately. | `services/transfer/executor_dicom.py:186-227`; `tests/test_transfer_executor.py:205-299` | 137 | Only the wrapper is dead. `download_scan_dicom` and `upload_scan_dicom` are live. |
| 5 | P34-F1, P33-F2, P35-F1, P39-F1 | `_expect_list` and `_expect_dict` are copied into five service modules. The messages have drifted: two copies say "GET" and are applied to POST responses. | `services/{commands,containers,docker_admin,events,scp}.py` | 75 | Make one shared helper that takes the verb. |
| 6 | P50-F1, P50-F2 | `Verifier.verify_resource` has no caller. `verify_scan_set` is copied inline as Tier 1 of `verify_experiment`. | `services/transfer/verifier.py:96-190` | 64 | Delete `verify_resource` (PR 2). `verify_scan_set` is also dead today. Make `verify_experiment` call it, which keeps the message tests (PR 3, a refactor). |
| 7 | P46-F1 | `ConflictChecker.check_experiment` is test-only. The pipeline checks subjects only. | `services/transfer/conflicts.py:52-65`; `tests/test_transfer_conflicts.py:79-122` | 58 | |
| 8 | P48-F3 | `_resolve_deferred_prearchive_blocking` repeats what `wait_for_archive` already does through `_poll_and_resolve_prearchive`. | `services/transfer/poller.py:405-450, 485-486` | 50 | Not behaviour-neutral. The resolver also runs after the timeout path (`poller.py:485`), and it passes timestamp `""` where `_archive_entry` skips timestamp-less entries. Write a blocking-drain test that pins both behaviours before removal. |
| 9 | P53-F2, P53-F3 | The windowed submit/wait/refill loop is written twice in gradual upload. The copy already shipped one bug, documented in the comment at lines 360-366. The progress block is also duplicated. | `services/upload/gradual.py:208-225, 263-405` | 40 | One `_run_windowed(...)` helper. |
| 10 | P02-F1 | `confirm_destructive` repeats the 30-line wrapper of `confirm_destructive_when`. | `cli/common/__init__.py:612-714` | 33 | `return confirm_destructive_when(lambda _: True, message)`. |
| 11 | P06-F1, F2, F3 | `api post` and `api put` repeat the body-routing block and the option stack. `_read_body` repeats its decode block. `raw_bytes` is carried only to be deleted. | `cli/api.py:65-235, 506-528, 632-658` | 35 | Test collateral: `TestDetectContentType` kwargs. |
| 12 | P52-F1, P51-F3 | `split_into_batches` is test-only. Both transports use `split_into_n_batches`. | `services/upload/shared.py:210-241`; `upload/__init__.py:52` | 35 | Six unit tests go too. |
| 13 | P28-F4, P28-F5 | `TransferStateStore.get_entities` and `_get_tables` are test-only. | `core/state.py:149-156, 346-370` | 33 | |
| 14 | P27-F1, P27-F2 | `print_info` and `create_spinner` are test-only. | `core/output.py:447-449, 476-486` | 27 | Keep the `create_progress` assertion. |
| 15 | P20-F1 | `_refresh_one` forwards seven arguments, and its docstring rationale is false. | `cli/xsync.py:320-344` | 26 | |
| 16 | P10-F1, F2, F3 | `dicom validate` and `dicom anonymize` repeat `_collect_dicom_files`. `_normalize_errors` only coerces values that are already strings. | `cli/dicom_cmd.py:20-26, 58-69, 352-365` | 24 | For `anonymize`, build the output path per file from `relative_to(input)`. |
| 17 | P15-F1, P15-R1 | `session download` builds `TransferSummary` at four sites with eight identical fields. A dedupe step does nothing. | `cli/session.py:325-345, 604-607, 662-678, 713-731` | 25 | The JSON shape stays the same. |
| 18 | P05-F1, P07-F3 | There are three copies of the `FILE|-` stdin reader and guard, and a second unified-diff helper. | `cli/anon.py:27-89`; `cli/payload.py:42-120`; `cli/common/batch.py:87-98` | 20 | |
| 19 | P49-F2, F3, F4, F5 | Scan pipeline: a negated single-use forwarder, invariant parameters, a duplicated finalize tail, and an except handler copied from the poller. | `services/transfer/scan_pipeline*.py` | 45 | F5 needs an adapter: `poller._record_experiment_failure` takes a `DeferredExperiment`. |
| 20 | P45-F2, P45-F3 | The experiment-resolution fallback ladder and the progress closure are duplicated between `download_resource` and `download_scans`. | `services/downloads/resource_scan.py:103-169` | 26 | |
| 21 | P46-F5, F6 | Discovery row loops are duplicated. `find_prearchive_entry` repeats `list_prearchive_entries`. | `services/transfer/discovery.py:126-197`; `executor_archive.py:59-65` | 24 | |
| 22 | P36-F1, P36-F2 | The `join_api_path` None-skip is test-only. `parse_resolved_experiment` has two near-identical branches. | `services/hierarchy.py:35-77, 528-555` | 19 | The detail branch keeps a `meta["xsi:type"]` fallback (line 539) that the summary branch lacks (line 553). The merge must keep it. |
| 23 | P41-F1, P22-F1 | The field-name validator is copied in `sessions.py` and `subjects.py`. `_has_dicom_magic` and the suffix set are copied in `core/exam.py` and `upload/shared.py`. | | 28 | The comment at `sessions.py:28-32` calls the copy deliberate. The PR must answer it. |
| 24 | P21-F1, F2 + P29-F1, F2 | Sync and async transport both have a no-op `except ...: raise`, a 403/404 branch that duplicates `>= 400`, and an identity expression. | `core/transport.py:441, 494-495, 589-590`; `core/async_transport.py:301, 357-358, 421-422` | 14 | Fix both twins together. Only the async file drops both imports; the sync file still uses `ResourceNotFoundError` (`transport.py:114`). |
| 25 | P02-F3, P01-F1, P24-F1, P22-F2 | Write-only `Context.no_color`, `no_headers` and `root_log_file`. `SESSION_CACHE_FILE` is defined twice, and the conftest patch of one copy does nothing. | `cli/common/__init__.py:198-201`; `cli/main.py:201,262-271`; `core/config.py:39`; `tests/conftest.py:316` | 15 | |
| 26 | V-2, P47-F1 | Two dead `except httpx.HTTPStatusError` handlers. `XNATClient` raises typed errors only. The docstrings that say "Raises: httpx.HTTPStatusError" are wrong too. | `services/transfer/executor_archive.py:300-309`; `services/transfer/executor_hierarchy.py:284-299` | 25 | Same root cause as Tier 0 row 11. Fix them in the same PR. |

Smaller production items, each under 15 lines, are in the appendix under "Accepted". Examples: P03-F1, P09-F1, P11-F1, P13-F1/F2, P18-F3/F4, P23-F1, P25-F1/F2, P27-F3, P29-F3/F4, P33-F1/F3, P38-F1, P43-F1/F2, P44-F1, P47-F3, P50-F3 to F7, P51-F1/F4, P54-F1/F2/F3/F5.

---

## Tier 2: test-suite cleanup

### 2a. Guard tests whose assertions cannot detect a missing guard (repair, do not delete)

This is the most important test-quality pattern. A test says "the prompt aborts, so nothing changes" or "a missing option is a usage error". Today the guard runs first and stops the command. But the test asserts only `exit_code != 0` and "no PUT/POST". The mock client is not configured, so if the guard is deleted, the command still fails later and the assertions still pass.

| IDs | Tests |
|---|---|
| T13-F1 to F5 | `tests/test_cli_session.py` share, vars-set abort, no pairs, duplicate key, missing `-E` |
| T16-F4 to F7 | `tests/test_cli_wrapper.py` enable, disable and config-set aborts, stdin guard |
| T15-R1, T15-R2 | `tests/test_cli_scp.py` create and disable aborts, AE-title and identifier guards |
| T09-F1, F2 | `tests/test_cli_container.py` duplicate `--param`, missing `=` (T09-R1, unknown wrapper ID, passes for the right reason; only add `exit_code == 5`) |
| T05-F1 | `tests/test_cli_admin.py:338-347` audit unavailable |
| T33-F1 | `tests/test_transfer_orchestrator.py:397-456` fault is injected where it is swallowed |
| T24-F1, T21-F1, T21-F2, T03-F5 | server-version gate, log file "no source", stderr parity, poller zero-vs-error bounds |

Fix: configure the mock for the happy path, so that only the guard can stop the command. Then assert the exact exit code and the message: 1 and "Aborted" for a declined prompt, 2 for a usage error, 5 for not found. T21-F2 (stderr parity) still needs a harness check before you change it.

### 2b. Test harness consolidation

| IDs | What | ~Lines |
|---|---|---|
| T10-F1, T06-F9, T12-R1, T03-F6 | Do this first. The `runner`, `_mock_config` and `_mock_client` trio is copied into five or more CLI test files, against the harness policy in `tests/conftest.py:3-10`. Migrate to `authenticated_cli`. This also removes most of the nested seam pairs in the next row. | 110+ |
| T01-F1, T01-F2, T14-R1 | Do this second. `core_config_seam` patches the same `Config.load` attribute as `config_seam`. Delete the second seam and the remaining nested `with` blocks. `AuthenticatedCLI.invoke` then delegates to `authenticated_seams`. The call-site count is not verified. | up to 150 |
| T19-F4, T27-F3, T33-F4, T33-F6, T36-F3, T29-F2, T34-R1, T34-R2, T37-F2 | Per-class and per-file copies of fixtures and spies. | 400 |
| T02-F1, T01-F4 | Four copies of the throwaway-project fixture in the integration tests. Make one factory in `tests/integration/conftest.py`. | 40 |
| T01-F5, T01-F7 | `temp_dir` duplicates the built-in `tmp_path`. `sample_patterns_json` has no user. | 25 |
| D-1, D-2 (dev only) | On `dev`, `_wire` in `tests/test_cli_session_labels.py:11-30` repeats `wire_rename_confirmation` in `tests/test_service_session_labels.py:20-51`. One test passes a redundant `put=None`. | 15 |

### 2c. Delete duplicate and tautological tests

Highest volume: `test_hierarchy_paths.py` (T20, 17 items), core-client transport tests (T17 and T18, 10 items; keep one of each twin), `test_cli_api.py` (T06), service 404 duplicates (T26 to T28, T37-F4), `test_package.py` import tautologies (T23-F3/F4/F5, about 85 lines), dataclass read-back tests (T04-F3, P24-F3, T34-F4, P46-F2), and the `CliRunner` importability tautologies (T04-F1, P27-F4). The full list is in the appendix. Coordinate the "keep the stronger copy" pairs: delete T03-F2, not T19-F3. Keep one of T17-F1 and T18-F4. T16-F9 and T17-F8 both require that the round-trip test and `test_save_creates_parent_dirs` stay.

---

## Tier 3: maintainer decisions

| ID | Question |
|---|---|
| P42-D1 | Which rename semantics are correct for the library: those of the service or those of the CLI? (Tier 1, item 1.) |
| P02-F4 | Twelve re-exports in `xnatctl.cli.common` have no user in the repository. They are a deliberate facade. At minimum, fix the false comment at lines 62-68. |
| P31-F3 | `Experiment = Session` is a Provisional compatibility alias with no user in the repository. |
| P32-D1 | `AdminService.refresh_catalogs` (a PUT per experiment) and the CLI fan-out (a POST to the refresh service) are two mechanisms for the same intent. |
| P28-F1, P30-F5, P21-F3 | Wire or delete: retry budget, timeout and worker validators, async write parameters. |

---

## Suggested pull-request sequence

Base every PR on `dev` (`f017cf7` or later), not `main`.

1. **PR 1, defects:** Tier 0 items 1, 2, 4, 5, 6, 7, 10, 11, plus Tier 1 item 26 (same root cause as item 11). These are small and each needs a regression test.
2. **PR 2, dead production code:** Tier 1 items 2, 3, 4, 7, 12, 13, 14, item 6 (`verify_resource` only), plus the small items. These names are Provisional and autodoc'd. Rebuild the API docs and add a CHANGELOG line for each removal.
3. **PR 3, duplication:** Tier 1 items 5, 6 (`verify_experiment` calls `verify_scan_set`), 8 (after its blocking-drain test), 9, 10, 11, 15 to 25.
4. **PR 4, test harness:** Tier 2b first (T10-F1, then T01-F1), then 2a, then 2c. Do 2b first, because it touches the same files.
5. **Design PRs after decisions:** subject-rename unification (Tier 1, item 1), the retry budget, the validators, and the envelope question.

Run `ruff`, `mypy --strict` and the full `pytest` after each PR. Several deletions orphan an import.

---

## Dev branch delta

`dev` changes three production files: `cli/session_labels.py`, `core/client.py` and `services/session_labels.py`. I read the full diff of the code, the tests and the changelog.

- **P40-F1 is removed.** The new `session normalize-labels --subject-pattern` passes the parameter that the plan called unused.
- **No other plan item changes.** P23-F1 in `client.py:385-389` is unaffected, because the dev change is after line 560.
- **New items:** D-1 and D-2 (Tier 2b).
- **New rename-confirmation code:** no slop. Every new constant is used, and nothing is left from the first `/xapi/workflows` approach. The single-shot PUT catches `(XNATConnectionError, ServerError)`, and `RequestTimeoutError` and `RetryExhaustedError` subclass `XNATConnectionError`. So a timed-out or 504'd PUT is confirmed through the history, as designed.

## Docs drift (outside the four categories)

- `docs/cli-reference.rst:489-490` and `docs/uploading.rst:480` say the `--wait` default is 900. `DEFAULT_ARCHIVE_WAIT_SECONDS` is 14400 (`core/timeouts.py:26`).
- `docs/adding-a-command.rst:397-399` says `@global_options` adds five flags; it adds seven. Lines 389-392 say there are two `confirm_destructive_when` uses; there are four.
- `docs/adding-a-command.rst:100, 503-504` cite `session_list` and `session_show` in `cli/session.py`; they live in `cli/session_query.py`.

## Rejected after audit (24)

The appendix lists all of them with reasons. Revision 2 added two: P40-F1 (invalidated on `dev`) and T12-F5 (its assertion discriminates). The main reasons: a live caller exists (T01-F6, T31-F2, P33-F4), the code holds a Stable contract pin (T17-F7, T26-F9, T26-F7, P48-F4), the change is not behaviour-preserving (P45-F5, P25-F3, P39-F3), the item is a deliberate named seam (P28-F6), or the item is version-dependent and cannot be settled from the repository (P52-F2, P40-F2).

# Appendix: every audited item

Columns: ID (scope-finding; V = verifier addition, D = dev branch), category, location at `a99e877`, estimated lines removed (agent estimate, after review corrections), lead audit note.


## Accepted findings (300)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| D-1 | - | (see note) | - | DEV: tests/test_cli_session_labels.py:11-30 _wire repeats tests/test_service_session_labels.py:20-51 wire_rename_confirmation. One helper with optional rows. ~15L. |
| D-2 | - | (see note) | - | DEV: redundant put=None in test_put_error_with_no_rename_event_reports_the_put_error. Trivial. |
| P02-F1 | redundant_abstraction | `xnatctl/cli/common/__init__.py:621` | 33 | HIGH: lead-read common/__init__.py:621-714; confirm_destructive == confirm_destructive_when(lambda _: True). ~33L. |
| P02-F2 | dead_code | `xnatctl/cli/common/__init__.py:88` | 1 | Unused _flag_given re-export + false comment. 1L. |
| P02-F3 | dead_code | `xnatctl/cli/common/__init__.py:198` | 10 | Lead-read: Context.no_color/no_headers/root_log_file write-only (global_options reads root_* and log_file only). Subsumes P01-F1. |
| P03-F1 | unnecessary_wrapper | `xnatctl/cli/common/deprecation.py:185` | 7 | Lead-read deprecation.py:171-190: pass-through wrapper; click.option(...)(f) equivalent. |
| P03-F3 | useless_test | `tests/test_exit_codes.py:256` | 5 | test_exit_codes.py:256-259 asserts Python builtin hierarchy only. |
| P05-F1 | redundant_abstraction | `xnatctl/cli/anon.py:27` | 18 | Read payload.py+anon.py: read_script_argument (anon.py:27-69) is line-for-line read_payload_argument minus JSON parse; _text_diff (anon.py:72-89) also duplicates json_diff's diff core. Merge into one FILE/- reader with optional parser. Net ~20L. |
| P05-F2 | dead_code | `xnatctl/cli/auth.py:174` | 0 | auth.py:174,200,232,280 fallback unreachable: global_options sets ctx.config=Config.load() (common/__init__.py:422) before body. Cosmetic, 0L net. |
| P06-F1 | dead_code | `xnatctl/cli/api.py:206` | 10 | raw_bytes param del'd at api.py:227; 4-tuple slot exists only for it. Test collateral: TestDetectContentType kwargs. |
| P06-F2 | redundant_abstraction | `xnatctl/cli/api.py:508` | 14 | api.py:508-528 == 643-658 verified; extract _route_body + shared option decorator. |
| P06-F3 | redundant_abstraction | `xnatctl/cli/api.py:114` | 9 | api.py:114-125 == 127-139 verified (narrowed: -d branch shares only JSON-or-raw). |
| P06-F4 | dead_code | `xnatctl/cli/api.py:409` | 1 | api.py:411 else arm unreachable. 1L. |
| P06-F5 | dead_code | `xnatctl/cli/api.py:54` | 2 | api.py:56 guard unreachable by regex construction. 2L. |
| P07-F4 | redundant_abstraction | `xnatctl/cli/config_cmd.py:60` | 8 | Narrowed: config_cmd.py:60-71 drifted copy of core.config._coerce_version; unify after hardening _coerce_version. Low. |
| P09-F1 | dead_code | `xnatctl/cli/custom_vars.py:324` | 8 | custom_vars.py:342-345 + 424-425 round trip confirmed. ~6L. |
| P10-F1 | redundant_abstraction | `xnatctl/cli/dicom_cmd.py:61` | 8 | dicom_cmd.py:58-69 == _collect_dicom_files(441-455). Read full file. |
| P10-F2 | redundant_abstraction | `xnatctl/cli/dicom_cmd.py:352` | 8 | dicom_cmd.py:352-365 collapses to comprehension over _collect_dicom_files. |
| P10-F3 | dead_code | `xnatctl/cli/dicom_cmd.py:20` | 7 | _normalize_errors (20-26): all errors are list[str] built locally; type results instead. ~8L. |
| P11-F1 | redundant_abstraction | `xnatctl/cli/pipeline.py:145` | 5 | pipeline.py:145-150 and ~194-199 identical closures. ~5L; trivial. |
| P12-F1 | dead_code | `xnatctl/cli/project.py:411` | 3 | HIGH VALUE: project.py:446 resolves workers, never read; orchestrator gets none. Stable flag is a silent no-op -> fix wiring or deprecate (not a delete). |
| P13-F1 | dead_code | `xnatctl/cli/resource.py:361` | 5 | Cross-check done: lead-read ResourceService.create 409 swallow (resources.py create, try/except ClientRequestError). CLI catch is redundant. |
| P13-F2 | redundant_abstraction | `xnatctl/cli/resource.py:223` | 5 | Lead-read resource.py:223-230 == _resolve_resource_parent session/scan branch. ~3L. |
| P13-R1 | dead_code | `xnatctl/cli/resource.py:376` | 1 | resource.py:376-377 dead store of progress label. Trivial. |
| P14-F1 | dead_code | `xnatctl/cli/scan_download.py:381` | 5 | Lead-read scan_download.py:185-189 vs 381-388: unreachable branch + wrong kept-zip filename (user-visible bug). Small. |
| P14-F2 | useless_test | `tests/test_cli_scan.py:1028` | 25 | Exact dup of test_all_resource_download.py::test_multiple_resources_rejected (agent-verified). |
| P15-F1 | redundant_abstraction | `xnatctl/cli/session.py:311` | 22 | Lead-read session.py:325-345,662-678,713-731: 4 TransferSummary sites repeat 8 fields; parameterise into one nested emitter. JSON shape unchanged. ~22L. |
| P15-R1 | dead_code | `xnatctl/cli/session.py:604` | 3 | Lead-read session.py:264-268 vs 604-607: scan_ok keyed by scan_id so dedupe is a no-op; stale comment. |
| P17-F1 | dead_code | `xnatctl/cli/session_upload_rest.py:249` | 4 | archive_workers no-op; pending root-cause check in rest_batch.py (P54). Collateral: test_upload_exit_codes.py positional call. |
| P18-F1 | redundant_abstraction | `xnatctl/cli/subject.py:535` | 65 | HIGH: lead-read subject.py:535-571, 605-629, 664-688: execute step x3 -> candidate-generation per branch + one execute loop. ~65L. |
| P18-F2 | redundant_abstraction | `xnatctl/cli/subject.py:641` | 3 | Lead-read subject.py:642-645 inlines _apply_template (used at 525). |
| P18-F3 | dead_code | `xnatctl/cli/subject.py:250` | 3 | Lead-read subject.py:250-253 redundant quiet branch (agent-verified enriched False when quiet). |
| P18-F4 | dead_code | `xnatctl/cli/subject.py:475` | 1 | Lead-read subject.py:475 vs 477 duplicate call. |
| P19-F1 | dead_code | `xnatctl/cli/upgrade.py:318` | 0 | Narrowed to bug: lead-read upgrade.py:318-336; missing member raises KeyError before the friendly UpgradeError. Translate KeyError. Not a deletion. |
| P20-F1 | unnecessary_wrapper | `xnatctl/cli/xsync.py:320` | 26 | Lead-read xsync.py:320-344, 513-523: pure single-use forwarder with false rationale. ~26L. |
| P20-F2 | dead_code | `xnatctl/cli/xsync.py:561` | 0 | xsync.py:561 always-true isinstance (agent-verified). Trivial. |
| P21-F1 | dead_code | `xnatctl/core/async_transport.py:421` | 4 | Lead-read async_transport.py:421-422 no-op except (+ sync twin transport.py:589-590). Drop 2 imports. |
| P21-F2 | dead_code | `xnatctl/core/async_transport.py:357` | 3 | Lead-read async_transport.py:357-358 == 391-392 for 403/404 (+ sync twin transport.py:494-495). |
| P21-F3 | dead_code | `xnatctl/core/async_transport.py:253` | 12 | Narrowed: only identity expr at async_transport.py:301 is unambiguous; unused write params are documented parity (decision). |
| P21-F4 | unnecessary_wrapper | `xnatctl/core/async_client.py:337` | 18 | Lead-read async_client.py:337-356 single-use forwarder. Low. |
| P22-F1 | redundant_abstraction | `xnatctl/core/exam.py:104` | 10 | Narrowed: byte-identical _has_dicom_magic + suffix set in core/exam.py and upload/shared.py; keep one. ~10L. Also docstring L44-45 drift. |
| P23-F1 | dead_code | `xnatctl/core/client.py:385` | 6 | Lead-read client.py:385-389; no caller per finder+reviewer; transport sets jar cookie directly (seen in async_transport.py:289-292). ~6L. |
| P24-F1 | dead_code | `xnatctl/core/config.py:39` | 2 | config.py:39 unread; conftest.py:316 patch of it is a no-op (lead saw raising=False patch). Delete both. |
| P24-F3 | useless_test | `tests/test_config.py:29` | 20 | Profile dataclass readback tests (test_config.py:29-48). Low. |
| P25-F1 | dead_code | `xnatctl/core/fsutil.py:71` | 4 | Optional: restrict_permissions bool return never consumed (7 call sites). Low. |
| P25-F2 | unnecessary_wrapper | `xnatctl/core/fsutil.py:50` | 8 | Optional: open_private single-caller one-liner. Low. |
| P26-F1 | dead_code | `xnatctl/core/logging.py:532` | 99 | HIGH: lead-read logging.py:532-626; inbound only same-module (layout map from full reads of all prod+test files); exported only via xnatctl.core.__all__ (Provisional), not xnatctl.__all__. ~99L + changelog note. |
| P26-F2 | unnecessary_wrapper | `xnatctl/core/logging.py:294` | 12 | get_logger becomes caller-less with F1; delete together (logging.py:294-303, core/__init__ 22,159). |
| P27-F1 | dead_code | `xnatctl/core/output.py:447` | 14 | print_info test-only; not in core __all__ (lead checked core/__init__ __all__). ~14L incl test. |
| P27-F2 | dead_code | `xnatctl/core/output.py:476` | 13 | create_spinner test-only; not in core __all__. ~13L. |
| P27-F3 | dead_code | `xnatctl/core/server_version.py:142` | 4 | Lead-read server_version.py:142-147 identical except bodies. |
| P27-F4 | useless_test | `tests/test_stream_discipline.py:122` | 4 | test_stream_discipline.py:122-123 tautology (same pattern as T04-F1). |
| P28-F1 | dead_code | `xnatctl/core/retry.py:210` | 80 | HIGH(decision): RetryBudget never constructed in prod (callers rest_archive.py:145,159, gradual_client.py:179, upload/resources.py:154 pass no budget). Wire or delete; ~125L incl tests. Also fix is_permanent_400 docstring retry.py:187-188 ('retry budget bounds the slow case'). |
| P28-F3 | dead_code | `xnatctl/core/retry.py:266` | 5 | _retry_after_seconds unreachable None guards (lead-read 266-282). Small. |
| P28-F4 | dead_code | `xnatctl/core/state.py:346` | 25 | TransferStateStore.get_entities test-only (~25L). |
| P28-F5 | dead_code | `xnatctl/core/state.py:149` | 8 | _get_tables test-only introspection. Low. |
| P29-F3 | dead_code | `xnatctl/core/update_check.py:42` | 1 | Unused urllib.error import. Trivial. |
| P29-F4 | unnecessary_wrapper | `xnatctl/core/update_check.py:227` | 9 | fetch_latest_version single-use pass-through; 5 tests patch it (retarget). Low. |
| P30-F1 | dead_code | `xnatctl/core/validation/paths.py:414` | 40 | HIGH: validate_archive_path + ALLOWED_ARCHIVE_EXTENSIONS: only re-export inbound (layout map + 2 traces). In xnatctl.core.__all__ (Provisional). ~40L. |
| P30-F2 | dead_code | `xnatctl/core/validation/paths.py:443` | 10 | validate_dicom_directory: re-export only. ~10L. |
| P30-F3 | dead_code | `xnatctl/core/validation/network.py:90` | 25 | validate_url_or_none: test-only. ~25L. |
| P30-F4 | dead_code | `xnatctl/core/validation/__init__.py:182` | 30 | validate_project_list: test-only. ~30L. |
| P30-F5 | dead_code | `xnatctl/core/validation/network.py:136` | 150 | validate_timeout/validate_workers test-only (~150L incl tests). Decision: wire into --timeout/--workers handling or delete; flags currently flow unvalidated. |
| P31-F1 | redundant_abstraction | `xnatctl/models/transfer.py:56` | 22 | Narrowed: ResourceFilter == ScanTypeFilter bodies; merge into one base, keep names as aliases. Low. |
| P31-F2 | useless_test | `tests/test_transfer_config.py:20` | 3 | Misnamed duplicate enum test. |
| P32-F1 | dead_code | `xnatctl/services/admin.py:269` | 3 | Lead-read admin.py:263-272 + base.py:78-98: 269-271 unreachable for both dicts and non-dicts. The raw envelope is a pinned Stable return shape (T26-F9), so: maintainer decision, see Tier 0 row 9. Same in pipelines.py:74-79,142-147. |
| P33-F1 | unnecessary_wrapper | `xnatctl/services/commands.py:253` | 11 | _wrappers_of pure delegator (2 call sites in one method). ~11L. |
| P33-F3 | unnecessary_wrapper | `xnatctl/services/commands.py:265` | 8 | _check_project_exists single-use. Low. |
| P34-F1 | redundant_abstraction | `xnatctl/services/containers.py:60` | 17 | _expect_list/_expect_dict copied in containers.py, docker_admin.py, events.py (and original in commands.py): one shared helper module (verb parameter). ~45L total. Merge P34-F2, P35-F1. |
| P34-R1 | dead_code | `xnatctl/services/docker_admin.py:89` | 0 | Unreachable default in _as_daemon_unreachable_error. Trivial. |
| P36-F1 | dead_code | `xnatctl/services/hierarchy.py:35` | 9 | Narrowed: join_api_path None-skip branch test-only; removing makes stray None fail loudly (fits module policy). ~9L. |
| P36-F2 | redundant_abstraction | `xnatctl/services/hierarchy.py:528` | 12 | Corrected: branches differ by meta['xsi:type'] fallback (hierarchy.py:539 vs 553); merge must keep the fallback. ~10L. |
| P38-F1 | unnecessary_wrapper | `xnatctl/services/resources.py:74` | 5 | _collection_path single-use staticmethod. ~5L. |
| P38-F2 | dead_code | `xnatctl/services/resources.py:411` | 0 | '.tar.gz' can never equal Path.suffix -> dead member and latent bug (.tar.gz never mapped to x-tar). Fix with name.endswith. |
| P39-F2 | dead_code | `xnatctl/services/scans.py:177` | 0 | Redundant `== ['*']` operand. Trivial. |
| P41-F1 | redundant_abstraction | `xnatctl/services/sessions.py:28` | 18 | _FIELD_NAME_RE/_validate_field_name identical in sessions.py and subjects.py; sessions.py:28-32 comment calls the copy deliberate - the PR must rebut it. ~18L. |
| P43-F1 | redundant_abstraction | `xnatctl/services/xsync.py:127` | 12 | save/check_credentials re-implement BaseService._post. Small. |
| P43-F2 | dead_code | `xnatctl/services/verify.py:140` | 2 | Unreachable 'not in candidates' guards. Low. |
| P44-F1 | dead_code | `xnatctl/services/downloads/transport.py:59` | 2 | Corrected wording: chunk_size is used at transport.py:95; no caller overrides the default. Trivial. |
| P44-R1 | test_only_seam | `xnatctl/services/downloads/__init__.py:10` | 2 | Test-only re-export alias _safe_output_path. Trivial. |
| P45-F1 | redundant_abstraction | `xnatctl/services/downloads/resource_scan.py:350` | 2 | Inline scans path == HierarchyService.build_scan_collection_path. Small. |
| P45-F2 | redundant_abstraction | `xnatctl/services/downloads/resource_scan.py:103` | 14 | Experiment-resolution fallback ladder duplicated in download_resource/download_scans. ~14L. |
| P45-F3 | redundant_abstraction | `xnatctl/services/downloads/resource_scan.py:157` | 12 | Identical progress_cb closures. ~12L. |
| P45-F4 | dead_code | `xnatctl/services/downloads/session.py:241` | 1 | Unused futures dict values. Trivial. |
| P46-F1 | dead_code | `xnatctl/services/transfer/conflicts.py:52` | 58 | ConflictChecker.check_experiment test-only (pipeline checks subjects only). ~58L incl tests. Resolves T34-F5. |
| P46-F2 | useless_test | `tests/test_transfer_conflicts.py:27` | 14 | Narrowed: TestConflictResult constructor tautology; ConflictResult.remote_id then unasserted and never read in prod (consider dropping field). ~14L. |
| P46-F3 | dead_code | `xnatctl/services/transfer/discovery.py:40` | 1 | ChangeType.DELETED never produced/compared. 1L. |
| P46-F4 | dead_code | `xnatctl/services/transfer/discovery.py:62` | 4 | DiscoveredEntity.parent_id write-only. Low. |
| P46-F5 | redundant_abstraction | `xnatctl/services/transfer/discovery.py:126` | 18 | discover_subjects/discover_experiments identical row loops. ~18L. |
| P46-F6 | redundant_abstraction | `xnatctl/services/transfer/executor_archive.py:59` | 6 | find_prearchive_entry repeats list_prearchive_entries GET+parse. ~6L. |
| P46-F7 | redundant_abstraction | `xnatctl/services/transfer/filter.py:57` | 10 | FilterEngine 3 methods share skeleton. Low. |
| P47-F1 | dead_code | `xnatctl/services/transfer/executor_hierarchy.py:284` | 13 | except httpx.HTTPStatusError unreachable (client raises typed XNATCtlError). ~13L + import. |
| P47-F2 | dead_code | `xnatctl/services/transfer/executor_dicom.py:186` | 137 | transfer_scan_dicom wrapper test-only (pipeline calls download/upload_scan_dicom directly). ~137L incl TestTransferScanDicom. Resolves T32-F1 (only wrapper dead, not mixin). |
| P47-F3 | dead_code | `xnatctl/services/transfer/executor_dicom.py:163` | 0 | Unreachable `else str(resp)`. Trivial. |
| P48-F1 | dead_code | `xnatctl/services/transfer/poller.py:124` | 14 | pending_count test-only seam. Low (keep if test value wanted). |
| P48-F2 | redundant_abstraction | `xnatctl/services/transfer/orchestrator.py:174` | 14 | Narrowed: use dataclasses.replace for two 10-line RETRY rebuilds. ~14L. |
| P48-F3 | redundant_abstraction | `xnatctl/services/transfer/poller.py:405` | 50 | Corrected (verifier): not only an error prefix. The blocking resolver runs at poller.py:485 also after the timeout path set archive_ready, and passes timestamp '' where _archive_entry skips timestamp-less entries. Write a blocking-drain test that pins both before removal. ~50L. |
| P49-F1 | dead_code | `xnatctl/services/transfer/scan_pipeline.py:459` | 2 | Always-true `not existing_id` checks. Small. |
| P49-F2 | unnecessary_wrapper | `xnatctl/services/transfer/scan_pipeline.py:501` | 19 | _scans_have_no_dicom single negated forward. ~19L. |
| P49-F3 | dead_code | `xnatctl/services/transfer/scan_pipeline_finalize.py:229` | 8 | Invariant params in _apply_xml_overlay. ~8L. |
| P49-F4 | redundant_abstraction | `xnatctl/services/transfer/scan_pipeline_finalize.py:111` | 12 | Narrowed: duplicated finalize tail; fold into _verify_and_record_experiment. ~12L. |
| P49-F5 | redundant_abstraction | `xnatctl/services/transfer/scan_pipeline.py:301` | 5 | Corrected: poller._record_experiment_failure takes a DeferredExperiment ctx; reuse needs an adapter or signature change. ~5L. |
| P50-F1 | dead_code | `xnatctl/services/transfer/verifier.py:96` | 31 | Lead-read verifier.py:96-238: verify_resource never called by verify_experiment; test-only. ~31L + tests. Resolves T36-F1. |
| P50-F2 | dead_code | `xnatctl/services/transfer/verifier.py:128` | 33 | Note: verify_scan_set is currently ALSO dead in prod; it becomes live only via the proposed refactor (verify_experiment calls it). Refactor, PR 3. |
| P50-F3 | dead_code | `xnatctl/services/transfer/scan_transfer.py:142` | 6 | transfer_scans progress_callback never read; 3 positional callers. ~6L. |
| P50-F4 | dead_code | `xnatctl/services/transfer/xml_overlay.py:160` | 3 | xnat_ns only forwarded recursively. Trivial. |
| P50-F5 | dead_code | `xnatctl/services/transfer/scan_transfer.py:55` | 3 | _DownloadedScan.resource_label write-only. Small. |
| P50-F6 | dead_code | `xnatctl/services/transfer/scan_transfer.py:454` | 3 | Narrowed: unreachable Optional/default handling in private helpers. Small. |
| P50-F7 | redundant_abstraction | `xnatctl/services/transfer/scan_transfer.py:272` | 10 | Narrowed: duplicated result folding (pooled vs serial); pooled path untested. ~10L. |
| P51-F1 | dead_code | `xnatctl/services/upload/resources.py:28` | 3 | Unused logger + import in upload/resources.py. Trivial. |
| P51-F4 | unnecessary_wrapper | `xnatctl/services/upload/archives.py:130` | 8 | Nested contextmanager factory -> single decorated generator. Small. |
| P52-F1 | dead_code | `xnatctl/services/upload/shared.py:210` | 35 | split_into_batches test-only (~35L + 6 tests); both transports use split_into_n_batches. |
| P53-F1 | redundant_abstraction | `xnatctl/services/upload/gradual.py:588` | 2 | Narrowed: outer pool.scope() no-op; rewrite docstrings with removal. |
| P53-F2 | redundant_abstraction | `xnatctl/services/upload/gradual.py:269` | 30 | HIGH: lead-read gradual.py:263-405: windowed submit/wait/refill loop duplicated; comment 360-366 documents a shipped drift bug. One _run_windowed(paths, workers, token, on_result) helper. ~30L. |
| P53-F3 | redundant_abstraction | `xnatctl/services/upload/gradual.py:208` | 10 | Duplicated progress-report block warmup/main. ~10L. |
| P54-F1 | dead_code | `xnatctl/services/upload/rest_batch.py:173` | 10 | Unreachable except OperationCancelledError (upload_single_archive converts it). ~10L. |
| P54-F2 | dead_code | `xnatctl/services/upload/rest_batch.py:59` | 5 | _UploadResult.cancelled test-only. Small. |
| P54-F3 | dead_code | `xnatctl/services/upload/rest_batch.py:55` | 9 | _UploadResult.duration/file_count write-only. ~9L. |
| P54-F4 | dead_code | `xnatctl/services/upload/rest_batch.py:203` | 0 | Lead-read rest_batch.py:193-229: archive_workers no-op on Stable UploadService path; docstring false. Deprecate param (warn), fix docstring; CLI/bench/helper plumbing (P17-F1, P55-F1) removed. Resolves P17-F1. |
| P54-F5 | dead_code | `xnatctl/services/upload/rest_batch.py:41` | 3 | DEFAULT_BATCH_SIZE unread; Provisional re-export. 3L. |
| P55-F2 | redundant_abstraction | `scripts/bench_transfer.py:252` | 12 | Narrowed: 3 copies of Result(...) construction in bench script. ~12L. |
| T01-F1 | redundant_abstraction | `tests/conftest.py:40` | 150 | HIGH(test infra): lead-verified conftest.py:39-41,145-146,182-186; cli.common.Config is core.config.Config, so core_config_seam double-patches same attr. Delete seam + ~150 call-site lines. |
| T01-F2 | redundant_abstraction | `tests/conftest.py:142` | 6 | AuthenticatedCLI.invoke can delegate to authenticated_seams after F1. |
| T01-F3 | dead_code | `tests/integration/conftest.py:142` | 4 | integration/conftest.py:168 `return None if last else None` loses diagnostics; surface in skip message. |
| T01-F5 | unnecessary_wrapper | `tests/conftest.py:213` | 8 | temp_dir duplicates builtin tmp_path; 5 user files. |
| T01-F7 | dead_code | `tests/conftest.py:266` | 16 | sample_patterns_json: zero users in layout map built from full reads of all 141 test files. Medium confidence. |
| T01-F8 | useless_test | `tests/integration/test_dicom_tls.py:149` | 1 | `assert crypto` lint workaround. |
| T02-F1 | redundant_abstraction | `tests/integration/test_prearchive_settings.py:33` | 40 | 3 verbatim fixtures (prearchive_settings:33-44, session_labels:26-41, sharing_and_vars:19-30) -> one conftest factory fixture. |
| T02-F2 | useless_test | `tests/integration/test_prearchive_settings.py:74` | 10 | prearchive_settings.py:74-83 duplicates parametrized [manual] case (PREARCHIVE_MODE_TO_CODE manual->0). |
| T02-F3 | redundant_abstraction | `tests/integration/test_search.py:108` | 7 | test_search.py:108-116 inlines saved_search fixture; also leaks on failure. |
| T02-R1 | dead_code | `tests/integration/test_golden_path.py:51` | 3 | golden_path.py:51,55 import+assert lint workaround. |
| T03-F2 | useless_test | `tests/test_all_resource_download.py:525` | 41 | Weaker dup of test_downloads.py::test_download_scan_delegates_to_download_resource. |
| T03-F3 | useless_test | `tests/test_api_surface.py:133` | 5 | Subset of subprocess-based test_lazy_imports check. |
| T03-F4 | useless_test | `tests/test_api_surface.py:151` | 15 | Subsumed by test_exceptions.py alias tests; drop _DEPRECATED_EXCEPTION_ALIASES constant too. |
| T03-F5 | useless_test | `tests/test_archive_poller.py:190` | 31 | Non-discriminating bounds (>=1,<=2) in poller test; rewrite to pin ==0 after errors. |
| T04-F1 | useless_test | `tests/test_audit_logger.py:327` | 8 | Lead-read test_audit_logger.py:327-330: tautology. |
| T04-F2 | dead_code | `tests/test_auth.py:321` | 31 | Lead-read auth.py:318-328; body == load_session(url) is not None (load_session clears expired + touches). No prod caller per finder+reviewer traces of all AuthManager consumers. Provisional surface. |
| T04-F3 | useless_test | `tests/test_auth.py:21` | 14 | Lead-read test_auth.py:21-34: dataclass readback. Low. |
| T04-F4 | dead_code | `tests/test_async_client.py:829` | 1 | Unused tmp_path param (agent-verified). Trivial. |
| T04-R1 | useless_test | `tests/test_auth.py:60` | 11 | test_auth.py:60-70 duplicates sliding-expiry twin (agent-verified). Low. |
| T04-R2 | dead_code | `tests/test_audit_logger.py:298` | 0 | Lead-read test_audit_logger.py:298,317: audit_log param unused (autouse fixture already isolates). Trivial. |
| T05-F1 | useless_test | `tests/test_cli_admin.py:338` | 10 | Read test 338-347 + admin_audit 241-250: bare Exception bypasses except clause; exit!=0 from handle_errors. Rewrite with XNATCtlError subtype + assert message (fix, not delete). |
| T06-F1 | useless_test | `tests/test_cli_api.py:434` | 21 | Dup of test_api_put_non_file_path_no_inbody. |
| T06-F2 | useless_test | `tests/test_cli_api.py:327` | 24 | Dup of flag-conventions new-spelling test + binary file test. |
| T06-F3 | useless_test | `tests/test_cli_api.py:654` | 28 | Narrowed: keep --file spelling, add header assert, drop -f twin (1032-1061). Coordinate with F4. |
| T06-F4 | useless_test | `tests/test_cli_api.py:686` | 26 | Narrowed: coordinate with F3; never delete both binary tests. |
| T06-F5 | useless_test | `tests/test_cli_api.py:849` | 37 | Dup POST -f compact JSON -t text/plain. |
| T06-F6 | useless_test | `tests/test_cli_api.py:1146` | 2 | Lead-reasoned: '-t' is substring of '--content-type'; vacuous assert. |
| T06-F7 | useless_test | `tests/test_cli_api.py:1321` | 3 | '-f'/'-d' satisfied by --log-file/--params help; vacuous. |
| T06-F8 | useless_test | `tests/test_cli_api.py:194` | 0 | Narrowed: strengthen with ResultSet payload (0 net lines). |
| T06-R1 | useless_test | `tests/test_cli_api.py:75` | 0 | Bracket test cannot detect naive split; rewrite. Low. |
| T07-F1 | useless_test | `tests/test_cli_admin_docker.py:162` | 8 | Agent-verified duplicate in test_cli_admin_docker.py:162-169. Low. |
| T07-F2 | useless_test | `tests/test_cli.py:130` | 20 | HARMFUL: lead-verified test_cli.py:133-149 asserts exit in (0,1); HOME env is inert because CONFIG_FILE is fixed at import (config.py:37-38), so `config init` can write the developer's real ~/.config/xnatctl/config.yaml. Delete. |
| T07-F3 | useless_test | `tests/test_cli.py:166` | 12 | Lead-read test_cli.py:157-176; zsh/fish strict dupes of test_completion.py; bash near-dupe. |
| T07-F4 | useless_test | `tests/test_cli_auth.py:182` | 12 | Lead-read test_cli_auth.py:182-193 subsumed by 293-316/318+. |
| T07-F5 | useless_test | `tests/test_cli_auth.py:263` | 20 | test_cli_auth.py:263-282 subsumed by byte-exact pin (agent-verified, same mocks seen). |
| T07-F6 | dead_code | `tests/test_auth_password_stdin.py:110` | 0 | Unused runner params (agent-verified). Trivial. |
| T08-F1 | useless_test | `tests/test_cli_config_cmd.py:16` | 8 | Full file read: _strip_ansi/_ANSI_RE/import re unused. |
| T08-F2 | useless_test | `tests/test_cli_common.py:234` | 3 | test_cli_common.py:234-235 identical to 208-209. |
| T08-F3 | useless_test | `tests/test_cli_config_cmd.py:51` | 16 | test_cli_config_cmd.py:51-66 mock Config + exit 0 only; real-Config path covered at 451-533. |
| T08-R1 | useless_test | `tests/test_cli_config_cmd.py:499` | 1 | Patch at line 499 targets pytest stdin; CliRunner swaps sys.stdin, so inert. Delete line. |
| T09-F1 | useless_test | `tests/test_cli_container.py:539` | 7 | Test passes for wrong reason (unstubbed get_json fails first). Rewrite to assert message. Agent-traced x2. |
| T09-F2 | useless_test | `tests/test_cli_container.py:547` | 7 | Same as F1 for '=' check. Rewrite. |
| T09-F3 | redundant_abstraction | `tests/test_cli_dicom_modify.py:20` | 31 | Narrowed: two non-identical DICOM writers; consolidate at pydicom floor. Low. |
| T09-R1 | useless_test | `tests/test_cli_container.py:513` | 0 | Corrected by verifier: get_json IS stubbed; test passes for the right reason. Only strengthen with exit_code == 5. |
| T09-R2 | useless_test | `tests/test_cli_dicom.py:161` | 0 | -t selection test satisfied by default listing; pick a different tag. |
| T10-F1 | redundant_abstraction | `tests/test_cli_pipeline.py:16` | 84 | Verified verbatim runner/_mock_config/_mock_client in test_cli_{pipeline,prearchive,api,project(+partial)} and runner in flag_conventions; conftest.py:3-10 policy says use authenticated_cli. Cross-cutting mechanical migration. |
| T11-F1 | useless_test | `tests/test_cli_scan.py:1540` | 15 | test_cli_scan.py:1540-1553 same argv/path as 136-155 with weaker assert. Low value (regression class). |
| T11-F2 | useless_test | `tests/test_cli_scan.py:157` | 14 | 157-170 strictly subsumed by 1573-1591. |
| T11-F3 | useless_test | `tests/test_cli_scan.py:222` | 24 | 222-245 only asserts 'T1w' in output; JSON pinned at 132-134. |
| T11-F4 | useless_test | `tests/test_cli_scan.py:247` | 0 | 247-270 '1' in output trivially true; tighten to strip()=='1'. |
| T12-F1 | useless_test | `tests/test_cli_project.py:565` | 14 | Agent-verified exact dup test_cli_project.py:565-578. |
| T12-F2 | useless_test | `tests/test_cli_project.py:365` | 0 | Narrowed: tighten vacuous assertion at test_cli_project.py:367 (keep test). |
| T12-F3 | useless_test | `tests/test_cli_resource.py:406` | 58 | Agent-verified strict subset of test_resource_upload.py file/dir tests (58L). |
| T12-F4 | useless_test | `tests/test_cli_resource.py:538` | 27 | Agent-verified weaker dup of test_resource_upload_failure_returns_click_exception. |
| T13-F1 | useless_test | `tests/test_cli_session.py:986` | 7 | Guard-test-passes-for-wrong-reason pattern (unconfigured get_json). Repair. |
| T13-F2 | useless_test | `tests/test_cli_session.py:1284` | 7 | Same pattern. Repair. |
| T13-F3 | useless_test | `tests/test_cli_session.py:1342` | 5 | Same pattern; assert exit 2 + message. |
| T13-F4 | useless_test | `tests/test_cli_session.py:1348` | 17 | Same pattern. |
| T13-F5 | useless_test | `tests/test_cli_session.py:1245` | 7 | Same pattern (validate_session_id(None) raises first). |
| T13-F6 | dead_code | `tests/test_cli_session.py:386` | 3 | Unused nonlocal call_count. |
| T14-R2 | vestigial_mock_misnamed_test | `?:1214` | 6 | Inert merge_subjects patch + misnamed test (test_cli_subject.py:1214-1261). |
| T15-F1 | useless_test | `tests/test_cli_transfer.py:61` | 13 | 3 --help smoke tests redundant with behavioural tests. Low. |
| T15-F2 | dead_code | `tests/test_cli_session_labels.py:8` | 0 | Unused date kwarg. Trivial. |
| T15-R1 | useless_test | `tests/test_cli_scp.py:181` | 0 | Same guard-test pattern for scp prompt-abort tests. Repair. |
| T15-R2 | useless_test | `tests/test_cli_scp.py:153` | 0 | Same pattern for scp validation tests. Repair. |
| T16-F1 | useless_test | `tests/test_cli_whoami_health.py:95` | 15 | Dup whoami json test. |
| T16-F10 | useless_test | `tests/test_config_secrets.py:77` | 4 | Low dup. |
| T16-F11 | useless_test | `tests/test_client_stream.py:30` | 6 | Narrowed: duplicate sleeps fixture. Low. |
| T16-F2 | useless_test | `tests/test_cli_whoami_health.py:167` | 14 | Dup ping json test. |
| T16-F3 | useless_test | `tests/test_cli_wrapper.py:67` | 0 | Narrowed: strengthen vacuous join assertion (0 net). |
| T16-F4 | useless_test | `tests/test_cli_wrapper.py:191` | 6 | Guard-test-passes-for-wrong-reason pattern (wrapper enable abort). Repair. |
| T16-F5 | useless_test | `tests/test_cli_wrapper.py:287` | 6 | Same pattern (disable abort). |
| T16-F6 | useless_test | `tests/test_cli_wrapper.py:390` | 12 | Same pattern (config set abort). |
| T16-F7 | useless_test | `tests/test_cli_wrapper.py:380` | 9 | Same pattern (stdin requires --yes). |
| T16-F9 | useless_test | `tests/test_config.py:151` | 24 | test_save_config subsumed by roundtrip; coordinate with T17-F8 (keep roundtrip + parent_dirs). |
| T17-F2 | useless_test | `tests/test_core_client_http.py:216` | 13 | Strict subset of timeouts-file connect-timeout test. |
| T17-F3 | useless_test | `tests/test_core_client_http.py:229` | 12 | Same as F2 + hierarchy-true assert. |
| T17-F4 | useless_test | `tests/test_core_client_http.py:242` | 11 | Strict subset of retry-file [GET] parametrized case. |
| T17-F5 | useless_test | `tests/test_core_client_http.py:192` | 13 | Dominated by retry-file connect-error ladder test. |
| T17-F8 | useless_test | `tests/test_config_versioning.py:382` | 12 | Weaker than test_save_config. |
| T18-F1 | useless_test | `tests/test_core_client_retry.py:164` | 17 | Exact dup of parametrized [429] Retry-After case. |
| T18-F2 | useless_test | `tests/test_core_client_retry.py:197` | 15 | Dup of parametrized [POST] read-timeout case. |
| T18-F3 | useless_test | `tests/test_core_client_timeouts.py:102` | 18 | Covered twice elsewhere; extra isinstance assert true by construction. |
| T18-F4 | useless_test | `tests/test_core_client_timeouts.py:87` | 14 | Twin of T17-F1: keep exactly one of the two authenticate-ConnectTimeout tests. |
| T18-F5 | useless_test | `tests/test_core_client_retry.py:298` | 28 | Hand-listed 11 httpx names subsumed by dynamic leaf walk test_it_is_translated. Drop orphan import. |
| T18-F6 | redundant_abstraction | `tests/test_core_client_retry.py:61` | 10 | Narrowed: _client injects private _client; use documented transport= seam; stale comment. Low. |
| T19-F1 | dead_code | `tests/test_downloads.py:519` | 0 | Narrowed: unused `outside` param in test helper. Trivial. |
| T19-F2 | useless_test | `tests/test_downloads.py:848` | 19 | Strict subset of identity test (agent-verified). |
| T19-F4 | useless_test | `tests/test_downloads.py:28` | 36 | 5 byte-identical fixture pairs in test_downloads.py; hoist to module scope. |
| T19-F5 | useless_test | `tests/test_downloads.py:25` | 17 | Constructor-stores-arg tautology. |
| T19-F6 | dead_code | `tests/test_downloads.py:359` | 20 | Vestigial zip/open patches force an error the test swallows; simplify. |
| T20-F1 | useless_test | `tests/test_hierarchy_paths.py:1653` | 3 | test_hierarchy_paths.py micro-dups (F1-F11, F13-F17, R1): accept as one batch; ~200L total. |
| T20-F10 | useless_test | `tests/test_hierarchy_paths.py:1302` | 9 | batch |
| T20-F11 | useless_test | `tests/test_hierarchy_paths.py:1734` | 6 | batch |
| T20-F13 | useless_test | `tests/test_hierarchy_paths.py:1537` | 5 | batch |
| T20-F14 | useless_test | `tests/test_hierarchy_paths.py:1067` | 19 | batch |
| T20-F15 | useless_test | `tests/test_hierarchy_paths.py:745` | 6 | batch |
| T20-F16 | useless_test | `tests/test_hierarchy_paths.py:307` | 4 | batch |
| T20-F17 | useless_test | `tests/test_hierarchy_paths.py:777` | 4 | batch |
| T20-F18 | useless_test | `tests/test_hierarchy_paths.py:753` | 9 | Tombstone test for removed build_path. Low. |
| T20-F19 | useless_test | `tests/test_hierarchy_paths.py:624` | 25 | Narrowed: 11 tests repeat stub DownloadService setup; helper. Low. |
| T20-F2 | useless_test | `tests/test_hierarchy_paths.py:1668` | 4 | batch |
| T20-F3 | useless_test | `tests/test_hierarchy_paths.py:1676` | 4 | batch |
| T20-F4 | useless_test | `tests/test_hierarchy_paths.py:441` | 4 | batch |
| T20-F5 | useless_test | `tests/test_hierarchy_paths.py:1979` | 5 | batch |
| T20-F6 | useless_test | `tests/test_hierarchy_paths.py:1378` | 12 | 3 duplicate zip-builder helpers. |
| T20-F7 | useless_test | `tests/test_hierarchy_paths.py:1774` | 3 | batch |
| T20-F8 | useless_test | `tests/test_hierarchy_paths.py:1350` | 20 | batch |
| T20-F9 | useless_test | `tests/test_hierarchy_paths.py:1916` | 48 | batch (48L) |
| T20-R1 | useless_test | `tests/test_hierarchy_paths.py:1691` | 3 | batch |
| T21-F1 | useless_test | `tests/test_log_file.py:828` | 11 | Asserts on a path never passed to CLI; rewrite to assert no file handler. |
| T21-F2 | useless_test | `tests/test_log_file.py:1053` | 0 | Narrowed: stderr-parity tests cannot see log-record stderr under pytest logging plugin; repair harness (lines 852, 968 too). |
| T21-F3 | useless_test | `tests/test_log_file.py:206` | 10 | Same inputs/asserts as test_typed_event_fields_are_carried. |
| T21-F4 | useless_test | `tests/test_log_file.py:174` | 4 | Near-dup of line 116 assertion. Low. |
| T21-F5 | dead_code | `tests/test_log_file.py:91` | 1 | Corrected wording: corr is read at :99; no caller overrides the default. Trivial. |
| T22-F2 | redundant_abstraction | `tests/test_exam_upload_classification.py:22` | 15 | Test-local Protocol + forwarding wrapper -> direct import. ~15L. |
| T23-F1 | useless_test | `tests/test_no_color.py:218` | 38 | Near-verbatim dup of transfer check failure test. |
| T23-F2 | useless_test | `tests/test_models_info.py:126` | 20 | Narrowed: replace subprocess test with in-process __all__ membership pin for ServerInfo/UserInfo. |
| T23-F3 | useless_test | `tests/test_package.py:9` | 5 | Lead-read test_package.py:9-12 hasattr __version__ dup. |
| T23-F4 | useless_test | `tests/test_package.py:63` | 32 | Lead-read test_package.py:63-94: import + `is not None` tautologies; conftest imports cli.main. |
| T23-F5 | useless_test | `tests/test_package.py:14` | 48 | Lead-read test_package.py:14-61 same pattern. |
| T24-F1 | useless_test | `tests/test_server_version.py:357` | 26 | Test cannot fail for named defect (gate skip). Rewrite or delete. |
| T24-F2 | useless_test | `tests/test_service_anon.py:117` | 0 | Narrowed: docstring overpromises; keep propagate pin. |
| T24-F4 | dead_code | `tests/test_resource_upload.py:87` | 2 | Dead spy branch. Trivial. |
| T25-F1 | useless_test | `tests/test_service_containers.py:185` | 10 | Mock-tautology on pass-through stream_logs. |
| T26-F1 | useless_test | `tests/test_service_hierarchy.py:534` | 3 | Dup empty-list test. |
| T26-F2 | useless_test | `tests/test_service_hierarchy.py:29` | 6 | Exact dup of test_hierarchy_paths::test_subject_path. |
| T26-F3 | useless_test | `tests/test_service_hierarchy.py:62` | 7 | Strict subset dup. |
| T26-F4 | useless_test | `tests/test_service_hierarchy.py:34` | 6 | Dup. |
| T26-F5 | useless_test | `tests/test_service_hierarchy.py:48` | 13 | Dup. |
| T26-F6 | useless_test | `tests/test_service_import.py:25` | 8 | Narrowed: archive_destination_params asserted in 2-3 files. |
| T26-F8 | useless_test | `tests/test_service_pipelines.py:81` | 7 | Weaker than typed-404 pin. |
| T27-F1 | useless_test | `tests/test_service_prearchive.py:86` | 7 | Dup 404 test. |
| T27-F2 | useless_test | `tests/test_service_projects.py:142` | 7 | Dup 404 test. |
| T27-F3 | useless_test | `tests/test_service_prearchive.py:21` | 28 | mock_client fixture copied in 4 service test files; hoist (conftest fake_client exists). |
| T28-F1 | useless_test | `tests/test_service_sessions.py:171` | 17 | Dup typed-404 dispatch test. |
| T28-F2 | useless_test | `tests/test_service_sessions.py:388` | 10 | Dup raw accessor test. |
| T29-F2 | useless_test | `tests/test_service_uploads_gradual_files.py:14` | 55 | Narrowed: inline client/service + 5 duplicate fake_upload_one closures; factor like scheduler file. ~55L. |
| T29-F3 | dead_code | `tests/test_service_upload_resource.py:59` | 1 | Unused status param. Trivial. |
| T30-F1 | useless_test | `tests/test_session_cache_perms.py:233` | 10 | Dup round-trip test. |
| T30-F2 | useless_test | `tests/test_session_cache_perms.py:124` | 10 | Dup round-trip test. Low. |
| T30-F3 | useless_test | `tests/test_service_uploads_gradual_scheduler.py:366` | 20 | Inline construction dup; use fixture. |
| T30-R1 | useless_test | `tests/test_service_uploads_parallel.py:411` | 6 | Duplicate inline mkdtemp tracker. Low. |
| T31-F1 | useless_test | `tests/test_session_download_failures.py:169` | 6 | Vacuous assertion. |
| T31-F3 | useless_test | `tests/test_session_download_failures.py:236` | 15 | Dup outcome test. |
| T31-F4 | dead_code | `tests/test_session_reauth_propagation.py:258` | 8 | Dead statement + redundant imports. |
| T31-F6 | redundant_abstraction | `tests/test_session_download_verify.py:380` | 38 | Duplicate download helper + inline invoke. ~38L. |
| T31-R1 | useless_test | `tests/test_session_reauth_propagation.py:189` | 6 | Dup test. |
| T33-F1 | useless_test | `tests/test_transfer_orchestrator.py:397` | 60 | Fault injected where swallowed: test cannot fail for named behaviour. ~60L; rewrite or delete. |
| T33-F2 | useless_test | `tests/test_transfer_orchestrator.py:1038` | 21 | Dup test. |
| T33-F3 | useless_test | `tests/test_transfer_orchestrator.py:906` | 23 | Duplicate response helpers. |
| T33-F4 | useless_test | `tests/test_transfer_orchestrator.py:324` | 90 | Narrowed: repeated executor-mock setup in 7 tests; factor. ~90L. |
| T33-F5 | useless_test | `tests/test_transfer_orchestrator.py:563` | 0 | Narrowed: throttle test is a smoke test; tighten assertion. 0L. |
| T33-F6 | useless_test | `tests/test_transfer_orchestrator.py:21` | 38 | Duplicate fixtures. ~38L. |
| T33-R1 | useless_test | `tests/test_transfer_orchestrator.py:164` | 2 | Dead setup. Trivial. |
| T34-F3 | dead_code | `tests/test_session_upload.py:551` | 2 | Redundant local import. Trivial. |
| T34-F4 | useless_test | `tests/test_transfer_conflicts.py:27` | 12 | Dataclass behaviour test. Low. |
| T34-R1 | useless_test | `tests/test_session_upload.py:98` | 30 | Duplicate upload helper. ~30L. |
| T34-R2 | useless_test | `tests/test_session_upload.py:320` | 50 | Duplicate gradual-mode helper. ~50L. |
| T35-F1 | redundant_abstraction | `tests/test_transfer_summary_json.py:839` | 14 | 4 copies of nested fake_download_session_fast; module helper. ~14L. |
| T36-F3 | useless_test | `tests/test_transfer_scan_transfer.py:19` | 45 | Narrowed: hand-rolled client/config fixtures across 4 transfer test files. ~45L. |
| T37-F2 | useless_test | `tests/test_upload_exam_cli.py:87` | 45 | Duplicated local spy/stub classes. ~45L. |
| T37-F3 | useless_test | `tests/test_upload_cancellation.py:317` | 12 | Narrowed: two near-identical UploadService builders. ~12L. |
| T37-R1 | dead_code | `tests/test_upload_exam_cli.py:216` | 19 | Dead spy method. ~19L. |
| T37-R2 | useless_test | `tests/test_upload_exam_cli.py:59` | 2 | Vacuous MISC assertion. Trivial. |
| T38-F1 | useless_test | `tests/test_validation_properties.py:567` | 4 | Dup. |
| T38-F2 | useless_test | `tests/test_validation_properties.py:585` | 3 | Dup. |
| T38-F3 | useless_test | `tests/test_validation_properties.py:589` | 1 | Narrowed: drop line 590 only. |
| T38-F4 | useless_test | `tests/test_validation_properties.py:595` | 3 | Dup. |
| T38-F5 | redundant_abstraction | `tests/test_uploaders_common.py:275` | 5 | Dup helper vs conftest make_response. |
| T38-F6 | dead_code | `tests/test_uploaders_common.py:146` | 0 | Unused params. Trivial. |
| T38-F7 | dead_code | `tests/test_validation_properties.py:457` | 1 | Unused return. Trivial. |
| T38-R1 | dead_code | `xnatctl/services/upload/shared.py:173` | 7 | collect_dicom_files redundant symlink guard (prod, shared.py:173-179) - verify with P52. |
| T39-F1 | useless_test | `tests/test_verify.py:755` | 11 | Dup test. |
| T39-F2 | useless_test | `tests/test_xsync_service.py:22` | 10 | Local copy of conftest.make_response. |
| T39-R1 | useless_test | `tests/test_xsync_cli.py:107` | 0 | Weak help assertion. Low. |
| T40-F1 | useless_test | `tests/test_zip_extract.py:164` | 0 | Narrowed: docstring overstates; keep test, fix docstring. 0L. |
| V-1 | - | (see note) | - | NEW (verifier, lead-verified): scan_transfer.py:619-626 `except httpx.HTTPStatusError` 409 tolerance is dead; create_scan uses self.dest.put (executor_hierarchy) which raises typed ClientRequestError, so a 409 fails the scan. Catch ClientRequestError with status_code 409. Latent bug. |
| V-2 | - | (see note) | - | NEW (verifier, lead-verified): executor_archive.py:300-309 dead `except httpx.HTTPStatusError` branch (archive_prearchive uses self.dest.post); generic except at 310 still logs. Delete branch; fix 'Raises: httpx.HTTPStatusError' docstrings. Same class as P47-F1. |

## Maintainer decisions (4)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| P02-F4 | dead_code | `xnatctl/cli/common/__init__.py:62` | 0 | 12 in-repo-unused re-exports on a pinned compatibility facade; maintainer call. Fix the false comment at 62-68 at minimum. |
| P31-F3 | dead_code | `xnatctl/models/session.py:63` | 4 | Experiment = Session compat alias; Provisional; maintainer call. |
| P32-D1 | - | (see note) | - | AdminService.refresh_catalogs (PUT per experiment) vs CLI fan-out via refresh_catalog (POST service): two mechanisms for one intent; Stable class, so design decision not cleanup. |
| P42-D1 | - | (see note) | - | HIGH: lead-read subjects.py:248-430. SubjectService.rename_batch/rename_pattern are a second, divergent rename engine (prefix match vs CLI fullmatch, no target validation, no {project}, 'merge' via rename not merge_subjects). CLI (P18-F1) reimplements inline. Recommend: move CLI engine into SubjectService, make CLI + rename_batch/rename_pattern call it (or deprecate the two methods). ~180L duplicated. |

## Merged into another finding (24)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| P07-F3 | redundant_abstraction | `xnatctl/cli/payload.py:73` | 15 | Merge with P05-F1: three copies of FILE/- stdin guard (batch.py:87-98, payload.py:73-84, anon.py:49-61) -> one helper. |
| P22-F2 | redundant_abstraction | `xnatctl/core/auth.py:31` | 1 | = P24-F1 (SESSION_CACHE_FILE defined twice). |
| P28-F2 | dead_code | `xnatctl/core/retry.py:220` | 8 | Into P28-F1 (_total unused, exhausted docstring inverted). |
| P29-F1 | dead_code | `xnatctl/core/transport.py:589` | 3 | Twin of P21-F1 (sync transport.py:589-590); fix both together. |
| P29-F2 | dead_code | `xnatctl/core/transport.py:441` | 0 | Twin of P21-F3 identity expr (transport.py:441). |
| P33-F2 | redundant_abstraction | `xnatctl/services/commands.py:25` | 75 | = P34-F1 (now 5 copies: commands, containers, docker_admin, events, scp; messages drifted - 'GET' wording on POST responses). |
| P34-F2 | redundant_abstraction | `xnatctl/services/docker_admin.py:95` | 17 | = P34-F1 |
| P35-F1 | redundant_abstraction | `xnatctl/services/events.py:54` | 45 | = P34-F1 |
| P37-F1 | dead_code | `xnatctl/services/pipelines.py:76` | 3 | Into P32-F1 pattern (pipelines get) — envelope-return pinned by T26-F9 tests; maintainer question. |
| P37-F2 | dead_code | `xnatctl/services/pipelines.py:144` | 3 | Into P32-F1 pattern (pipelines status). |
| P39-F1 | redundant_abstraction | `xnatctl/services/scp.py:22` | 22 | = P34-F1 |
| P51-F2 | dead_code | `xnatctl/services/upload/__init__.py:41` | 3 | = P54-F5 (DEFAULT_BATCH_SIZE unread). |
| P51-F3 | dead_code | `xnatctl/services/upload/__init__.py:52` | 90 | = P52-F1 (split_into_batches test-only). |
| P52-R1 | dead_code | `xnatctl/services/upload/shared.py:173` | 7 | = T38-R1 (collect_dicom_files symlink block). |
| P55-F1 | dead_code | `scripts/bench_transfer.py:338` | 1 | Into P17-F1 (archive_workers no-op root cause in rest_batch). |
| T01-F4 | redundant_abstraction | `tests/integration/test_anon.py:62` | 30 | Merge with T02-F1 (shared throwaway-project factory incl. test_anon.fresh_project). |
| T03-F6 | redundant_abstraction | `tests/test_api_get_fallback.py:16` | 30 | Merge into T10-F1 harness migration. |
| T06-F9 | redundant_abstraction | `tests/test_cli_api.py:17` | 28 | Merge into T10-F1 harness migration. |
| T12-R1 | duplicate_test_helper | `tests/test_cli_project.py:42` | 87 | Merge into T10-F1 harness migration (29 inline mock-client stubs). |
| T14-R1 | redundant_test_setup | `?:11` | 45 | Double Config.load patch (core_config_seam+config_seam patch same attr) across 44 tests; merge into T10-F1; note conftest AuthenticatedCLI.invoke:145-146 same. |
| T24-F3 | useless_test | `tests/test_service_admin.py:192` | 15 | Into P32-F1 (get_user ResultSet test pins unreachable branch). |
| T26-R1 | dead_code | `xnatctl/services/pipelines.py:76` | 6 | PipelineService.get/status unreachable extract (pipelines.py:76-78,144-146) — same as P32-F1 pattern; envelope-return is pinned by tests (T26-F9), so treat as maintainer question, not silent delete. |
| T37-F1 | dead_code | `tests/test_upload_400_policy.py:172` | 85 | = P28-F1 (RetryBudget wire-or-delete). |
| T39-F3 | useless_test | `tests/test_xsync_service.py:17` | 4 | Into T39-F2. |

## Duplicates of another finding (29)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| P01-F1 | dead_code | `xnatctl/cli/main.py:201` | 2 | = P02-F3 |
| P03-F2 | useless_test | `tests/test_cli_common.py:234` | 3 | = T08-F2 |
| P07-F1 | useless_test | `tests/test_cli.py:130` | 25 | = T07-F2 |
| P07-F2 | useless_test | `tests/test_cli_config_cmd.py:51` | 16 | = T08-F3 |
| P22-F3 | dead_code | `xnatctl/core/auth.py:318` | 29 | = T04-F2 |
| P22-F4 | useless_test | `tests/test_auth.py:21` | 14 | = T04-F3 |
| P24-F2 | redundant_abstraction | `xnatctl/cli/config_cmd.py:57` | 7 | = P07-F4 |
| P34-F4 | useless_test | `tests/test_service_containers.py:185` | 10 | = T25-F1 |
| P35-F2 | useless_test | `tests/test_session_upload.py:310` | 10 | = T26-F6 |
| P36-F3 | useless_test | `tests/test_service_hierarchy.py:29` | 12 | = T26-F2 + T26-F3 |
| P37-F3 | useless_test | `tests/test_service_prearchive.py:86` | 6 | = T27-F1 |
| P37-F4 | useless_test | `tests/test_service_pipelines.py:81` | 6 | = T26-F8 |
| P45-F6 | useless_test | `tests/test_downloads.py:525` | 40 | = T03-F2 (delete the weaker test_all_resource_download.py copy). |
| T03-F1 | useless_test | `tests/test_all_resource_download.py:328` | 30 | = P14-F2 (keep one copy of multiple-resources-rejected test). |
| T16-F8 | useless_test | `tests/test_config.py:29` | 20 | = P24-F3 |
| T17-F1 | useless_test | `tests/test_core_client_http.py:318` | 9 | = T18-F4 (twin; keep one) |
| T17-F6 | useless_test | `tests/test_core_client_retry.py:298` | 29 | = T18-F5 |
| T22-F1 | useless_test | `tests/test_exit_codes.py:256` | 5 | = P03-F3 |
| T29-F1 | useless_test | `tests/test_service_sessions.py:171` | 17 | = T28-F1 |
| T31-F5 | useless_test | `tests/test_session_reauth_propagation.py:71` | 6 | Twin of T04-R1 (keep one no-deadline test). |
| T32-F1 | dead_code | `tests/test_transfer_executor.py:205` | 137 | = P47-F2 |
| T34-F1 | useless_test | `tests/test_stream_discipline.py:122` | 5 | = P27-F4 |
| T34-F2 | useless_test | `tests/test_session_upload.py:310` | 10 | = T26-F6 |
| T34-F5 | dead_code | `tests/test_transfer_conflicts.py:79` | 58 | = P46-F1 |
| T34-F6 | dead_code | `tests/test_state_store.py:27` | 8 | = P28-F5 |
| T34-F7 | dead_code | `tests/test_state_store.py:110` | 25 | = P28-F4 |
| T36-F1 | dead_code | `tests/test_transfer_verifier.py:35` | 70 | = P50-F1 |
| T36-F2 | dead_code | `tests/test_transfer_verifier.py:76` | 67 | = P50-F2 |
| T36-R1 | dead_code | `xnatctl/services/transfer/scan_pipeline.py:459` | 1 | = P49-F1 |

## Doc and comment notes (outside the four categories) (20)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| P01-R1 | stale_comment | `xnatctl/cli/main.py:203` | 0 | Stale comments main.py:203-205 and 257-260 confirmed by reading main.py (whoami has @global_options; 218-225 lists undecorated commands). Comment fix only. |
| P04-R1 | - | `?:126` | 0 | Comment-only (admin.py:126-129 misdescribes fan-out). Cross-check AdminService.refresh_catalogs duplicate fan-out in P32. |
| P07-R1 | stale_comment | `xnatctl/cli/payload.py:3` | 0 | payload.py docstring omits event.py consumer. |
| P10-R1 | stale_comment | `xnatctl/cli/scp.py:126` | 0 | Comment-only scp.py:126-128. |
| P12-R1 | stale_comment | `xnatctl/cli/project.py:253` | 3 | project grant docstring 253-255 stale roadmap note (user-visible --help). |
| P16-S1 | - | (see note) | - | session_upload_dicom.py:67 user message names nonexistent --dicom-host (option is --host); docs/adding-a-command.rst:100,504 cite session_list/show in wrong module. |
| P20-R1 | stale_comment | `xnatctl/cli/xsync.py:17` | 0 | xsync.py:17-18 docstring contradicts confirm-once behavior. |
| P20-R2 | stale_comment | `xnatctl/cli/xsync.py:470` | 0 | xsync refresh-credentials --help example garbled (user-visible). |
| P24-R1 | stale_comment | `xnatctl/core/config.py:125` | 0 | config.py:125-127 stale '(schema version can come later)'. |
| P28-R1 | stale_comment | `xnatctl/core/retry.py:251` | 0 | _retry_after_seconds docstring vs '-5' handling. |
| P30-R1 | stale_comment | `xnatctl/core/validation/paths.py:58` | 0 | paths.py:58-59, 45-46 misleading docstrings. |
| P33-R1 | stale_comment | `xnatctl/services/commands.py:623` | 3 | wrapper_enabled_state docstring names nonexistent caller. |
| P34-F3 | unnecessary_wrapper | `xnatctl/services/containers.py:137` | 0 | Stale 'a later slice' docstrings in ContainerService. |
| P39-R1 | stale_comment | `xnatctl/services/scp.py:49` | 0 | Dangling docstring references scp.py:49-52, search.py:46-52. |
| P39-R2 | stale_comment | `xnatctl/services/search.py:132` | 3 | History narration search.py:132-134. |
| P51-R1 | stale_comment | `xnatctl/services/upload/resources.py:213` | 1 | resources.py:213-216 false caller claim. |
| P51-R2 | stale_comment | `xnatctl/services/upload/__init__.py:4` | 1 | upload/__init__.py:4-6 docstring lists nonexistent transport. |
| T09-R3 | stale_comment | `tests/test_cli_dicom.py:3` | 6 | test_cli_dicom.py:3-9 stale docstring (no dicom extra, no skip). |
| T09-R4 | stale_comment | `tests/test_cli_dicom_modify.py:284` | 7 | test_cli_dicom_modify.py orphan banners 284-286, 320-323. |
| V-3 | - | (see note) | - | Docs drift (verifier): docs/cli-reference.rst:489-490 and docs/uploading.rst:480 say --wait default 900, but DEFAULT_ARCHIVE_WAIT_SECONDS = 14400 (core/timeouts.py:26); docs/adding-a-command.rst:397-399 says @global_options adds five flags (seven); :389-392 says two confirm_destructive_when uses (four). |

## Rejected after audit (24)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| P25-F3 | redundant_abstraction | `xnatctl/core/redact.py:48` | 15 | Refuted: different contracts (parsed netloc vs lexical regex). |
| P28-F6 | unnecessary_wrapper | `xnatctl/core/retry.py:201` | 0 | Narrowed to docstring fix ('5xx' overstated); keep named policy predicate. |
| P33-F4 | dead_code | `xnatctl/services/anon.py:73` | 9 | Refuted: documented method + integration fixture use. |
| P35-F3 | useless_test | `tests/test_service_events.py:151` | 15 | Refuted: pins Raises contract. |
| P38-F3 | dead_code | `xnatctl/services/resources.py:72` | 0 | Narrowed to not-worth-it. |
| P39-F3 | unnecessary_wrapper | `xnatctl/services/scans.py:21` | 5 | Refuted: validates via ExperimentRef construction. |
| P40-F1 | dead_code | `xnatctl/services/session_labels.py:134` | 7 | Invalidated on dev: commit 3a8428d adds --subject-pattern and the CLI passes subject_pattern. |
| P40-F2 | dead_code | `xnatctl/services/session_labels.py:129` | 28 | Uncertain: documented options; depends on planned CLI flags. |
| P45-F5 | redundant_abstraction | `xnatctl/services/downloads/session.py:164` | 12 | Refuted: not behaviour-preserving. |
| P48-F4 | dead_code | `xnatctl/services/transfer/orchestrator.py:67` | 3 | Refuted: dry_run is the documented marker for overloaded subjects_skipped. |
| P52-F2 | dead_code | `xnatctl/services/upload/dicom_store.py:50` | 20 | Uncertain: pynetdicom version-dependent fallback. |
| T01-F6 | dead_code | `tests/conftest.py:207` | 5 | Refuted: used by test_cli_container.py (also confirmed by layout map). |
| T12-F5 | useless_test | `tests/test_cli_project.py:673` | 3 | Verifier: the audit assertion is the test's only discriminating check; keep it. |
| T12-F6 | useless_test | `tests/test_cli_project.py:711` | 7 | Help-prose guard is a legitimate contract check; uncertain verdict. |
| T17-F7 | useless_test | `tests/test_core_client_http.py:91` | 5 | Refuted: pins transport=None default which keeps verify handling. |
| T19-F3 | useless_test | `tests/test_downloads.py:232` | 0 | Reviewer: this copy is the stronger one; delete the weaker T03-F2 copy instead. |
| T20-F12 | useless_test | `tests/test_hierarchy_paths.py:1764` | 10 | Refuted: only test of subject-without-label archive branch. |
| T22-F3 | useless_test | `tests/test_lazy_imports.py:111` | 0 | Narrowed to misnaming only; distinct from-import + cold-order coverage. |
| T25-F2 | useless_test | `tests/test_service_containers.py:213` | 18 | Refuted: live documented delegation. |
| T26-F7 | useless_test | `tests/test_service_import.py:21` | 3 | Refuted: shared wire constant pin. |
| T26-F9 | useless_test | `tests/test_service_pipelines.py:66` | 24 | Refuted: characterization pin of Stable pipeline status JSON shape. |
| T31-F2 | dead_code | `tests/test_session_download_failures.py:42` | 10 | Refuted: imported by another test module with hostile=True. |
| T36-F4 | useless_test | `tests/test_update_check.py:385` | 7 | Refuted: pins first-run scenario. |
| T37-F4 | useless_test | `tests/test_upload_gradual_pool.py:119` | 0 | Narrowed to keep. |

## Clean scopes (3)

| ID | Category | Location | ~Lines | Lead audit note |
|---|---|---|---|---|
| P08-ZERO | - | (see note) | - | Clean scope; finder+reviewer agree; rejected candidates reviewed by lead. |
| P16-ZERO | - | (see note) | - | Clean; session_upload.py shim justified (registration + test imports). Lead reviewed rejections. |
| P42-ZERO | - | (see note) | - | No deletable findings (Stable class). |
