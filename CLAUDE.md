# CLAUDE.md

## Project Overview

xnatctl is a modern CLI for XNAT neuroimaging server administration. It provides resource-centric commands with consistent output formats, parallel operations, and profile-based configuration.

## Directory Structure

```
xnatctl/
├── xnatctl/                  # Python package
│   ├── __init__.py           # Package exports, __version__
│   ├── __main__.py           # Allow `python -m xnatctl`
│   ├── py.typed              # PEP 561 marker
│   ├── cli/                  # Click CLI commands
│   │   ├── main.py           # Root CLI group + whoami, health, completion
│   │   ├── common.py         # Shared decorators, Context class, output helpers
│   │   ├── auth.py           # auth login/logout/status/test
│   │   ├── config.py         # CLI config helpers (NOT the config command)
│   │   ├── config_cmd.py     # config init/show/use-context/add-profile
│   │   ├── project.py        # project list/show/create
│   │   ├── subject.py        # subject list/show/rename/delete
│   │   ├── session.py        # session list/show/download/upload (+ upload-dicom)
│   │   ├── scan.py           # scan list/show/delete/download
│   │   ├── resource.py       # resource list/show/upload/download
│   │   ├── prearchive.py     # prearchive list/archive/delete/rebuild/move
│   │   ├── pipeline.py       # pipeline list/run/status/jobs/cancel
│   │   ├── admin.py          # admin refresh-catalogs/user/audit
│   │   ├── api.py            # api get/post/put/delete (escape hatch)
│   │   └── dicom_cmd.py      # dicom validate/inspect (optional extra)
│   ├── core/                 # HTTP client, config/auth, output, validation
│   │   ├── client.py         # XNATClient (httpx, retry, pagination)
│   │   ├── config.py         # YAML profiles + env overrides
│   │   ├── auth.py           # AuthManager, session token caching (~/.config/xnatctl/.session)
│   │   ├── timeouts.py       # DEFAULT_HTTP_TIMEOUT_SECONDS = 21600 (6 hours)
│   │   ├── exceptions.py     # Exception hierarchy (XNATCtlError base)
│   │   ├── validation.py     # Input validators (URLs, XNAT IDs, DICOM AE titles)
│   │   ├── logging.py        # Logging utilities + AuditLogger
│   │   └── output.py         # Output formatting (json/table/quiet) via Rich
│   ├── models/               # Pydantic v2 models for XNAT resources
│   │   ├── base.py           # BaseModel config + XNATResource mixin
│   │   ├── progress.py       # UploadProgress, DownloadProgress, OperationPhase
│   │   ├── project.py        # Project model
│   │   ├── subject.py        # Subject model
│   │   ├── session.py        # Session model
│   │   ├── scan.py           # Scan model
│   │   └── resource.py       # Resource + ResourceFile models
│   └── services/             # Service layer encapsulating XNAT REST calls
│       ├── base.py           # BaseService(_get, _post, _paginate, _extract_results)
│       ├── projects.py       # ProjectService
│       ├── subjects.py       # SubjectService
│       ├── sessions.py       # SessionService
│       ├── scans.py          # ScanService
│       ├── resources.py      # ResourceService
│       ├── downloads.py      # DownloadService (streaming, checksum, ZIP extraction)
│       ├── uploads.py        # UploadService (batch REST, parallel REST, DICOM C-STORE)
│       ├── prearchive.py     # PrearchiveService
│       ├── pipelines.py      # PipelineService
│       └── admin.py          # AdminService
├── docs/                     # Sphinx documentation (user guide + API reference)
├── scripts/                  # Maintenance scripts (not part of core CLI)
│   ├── apply_label_fixes.py  # Subject + experiment label normalization helper
│   └── example_patterns.json # Anonymized rename rules reference
├── tests/                    # Pytest test suite
├── pyproject.toml
└── uv.lock
```
## Branching Strategy

```
main       ●───────────────●─────────── (stable releases, tagged)
            \             /
dev         ●──●──●──●──●──●──●─────── (daily integration)
                \     /       \
feat/*, fix/*    ●──●          ●──●──── (short-lived feature/fix branches)
```

| Branch | Purpose | Merges to |
|--------|---------|-----------|
| `main` | Stable releases only. Tagged with semver. | - |
| `dev` | Daily integration. All work lands here first. | `main` (on user request) |
| `feat/*`, `fix/*` | Short-lived branches off `dev`. | `dev` |

- **Version bumps and releases happen only when the user explicitly requests it.**
- Merging `dev` -> `main` triggers the release workflow (version bump, changelog, Docker build, tag).

## Development

## Development Commands

```bash
# Install for development
uv sync --dev

# Run CLI
uv run xnatctl --help

# Run tests
uv run pytest tests/ -v

# Run a single test
uv run pytest tests/test_cli.py::test_name -v

# Lint and format
uv run ruff check xnatctl scripts
uv run ruff format xnatctl scripts

# Type check
uv run mypy xnatctl

# Build docs
uv sync --extra docs
cd docs && make html
```

## Architecture

### Layered Design

```
CLI (Click commands + decorators)  -->  Services (REST operations)  -->  Client (httpx)
         |                                    |                              |
     common.py Context                   BaseService                   XNATClient
     @global_options                     _get/_post/_paginate          retry/auth/pagination
     @require_auth                                                     auto-reauth on 401
     @handle_errors
     @confirm_destructive
```

### CLI Decorator Stack

Commands compose these decorators (order matters):

```python
@project.command("list")
@global_options          # --profile, --output, --quiet, --verbose; sets up Context
@require_auth            # Ensures authenticated client (auto-reauth on expired session)
@handle_errors           # Catches XNATCtlError -> print_error() + sys.exit(1)
def project_list(ctx: Context) -> None: ...
```

Additional decorators for destructive/batch commands:
- `@confirm_destructive(message)` -- adds `--yes/-y` and `--dry-run` flags
- `@parallel_options` -- adds `--parallel/--no-parallel` and `--workers` flags
- `@batch_option` -- adds `--batch FILE` for bulk operations

### Service Layer

Services extend `BaseService(client)` and wrap client HTTP methods:

```python
from xnatctl.core.client import XNATClient
from xnatctl.services.projects import ProjectService

client = XNATClient(base_url="https://xnat.example.org", ...)
client.authenticate()

service = ProjectService(client)
projects = service.list()
```

### Pydantic Models

- Base: `populate_by_name=True` (aliases for XNAT API fields like `subject_ID`), `extra="ignore"`, `str_strip_whitespace=True`
- `XNATResource` mixin: `id` (alias `ID`), `label`, `uri` (alias `URI`)
- Each model has `table_columns()` for Rich table output and `to_row(columns)` for extraction

## Key Design Principles

1. **Resource-centric**: `xnatctl <resource> <action> [args]`
2. **Consistent output**: Commands support `--output json|table` and `--quiet` (ID-only)
3. **Ops safety**: `--dry-run` for destructive operations, `--yes` to skip confirmation
4. **Profile-based config**: Switch environments with `--profile`
5. **Pure HTTP**: Direct REST API calls with httpx, no pyxnat
6. **Parallel by default**: Batch operations use `ThreadPoolExecutor`

### Parent-Resource Options Convention

`session` and `scan` commands use uniform `-P`/`-E` options for parent-resource scoping:

| Option | Long | Applies to | Required | Description |
|--------|------|-----------|----------|-------------|
| `-P` | `--project` | session + scan commands | No | Project ID. Enables experiment lookup by label. Falls back to `default_project` from profile. |
| `-S` | `--subject` | `session list` (filter), `session upload`, all `scan` commands | No | Subject ID/label. Narrows experiment lookup (requires `-P`). |
| `-E` | `--experiment` | `session show/download`, all `scan` commands | Yes | Experiment ID (accession #) or label. Labels require `-P` (explicit or via profile default). |

**ID vs label resolution**:
- `-E` alone (no `-P`): value must be an experiment accession ID (e.g., `XNAT_E00001`), routed to `/data/experiments/{id}`
- `-E` with `-P`: value can be accession ID or experiment label, routed to `/data/projects/{P}/experiments/{E}`
- `-E` with `-P` and `-S`: routed to `/data/projects/{P}/subjects/{S}/experiments/{E}`
- If `-P` is omitted but the active profile has `default_project`, that project is used automatically

## Coding Style

- Python 3.11+; type hints throughout
- Click for CLI framework
- Pydantic v2 for data models
- httpx for HTTP client
- Rich for output formatting
- Ruff for linting/formatting (line-length=100, target py311, selects E/F/W/I/B/UP)
- mypy for type checking (check_untyped_defs, ignore_missing_imports)

## Configuration

Config file: `~/.config/xnatctl/config.yaml`
Session cache: `~/.config/xnatctl/.session` (JSON, chmod 0o600, 15-min expiry)

Profile fields:
- `url` - XNAT server URL (required)
- `username`, `password` - Credentials (optional, can use env vars instead)
- `verify_ssl` - SSL verification (default: true)
- `timeout` - Request timeout in seconds (default: 30)
- `default_project` - Default project ID (optional)

Environment variables:
- `XNAT_URL`, `XNAT_USER`, `XNAT_PASS` - Server credentials
- `XNAT_TOKEN` - Session token (highest auth priority, skips credential prompt)
- `XNAT_PROFILE` - Active profile name
- `XNAT_VERIFY_SSL` - Override SSL verification (`true`/`false`)
- `XNAT_TIMEOUT` - Override HTTP timeout seconds

Credential priority: CLI args > env vars > profile config > prompt

## Exception Hierarchy

All exceptions extend `XNATCtlError(message, details: dict)`:
- **Config**: `ConfigurationError`, `ProfileNotFoundError`
- **Validation**: `ValidationError`, `InvalidURLError`, `InvalidIdentifierError`, `PathValidationError`
- **Connection**: `ConnectionError`, `NetworkError`, `TimeoutError`, `RetryExhaustedError`
- **Auth**: `AuthenticationError`, `SessionExpiredError`, `PermissionDeniedError`
- **Resources**: `ResourceNotFoundError`, `ResourceExistsError`
- **Operations**: `UploadError`, `DownloadError`, `BatchOperationError`
- **DICOM**: `DicomError`, `DicomParseError`, `DicomStoreError`

## Gotchas

- **`cli/config.py` vs `cli/config_cmd.py`**: `config_cmd.py` is the Click `config` command group. `config.py` is a separate CLI config helper module. Don't confuse them.
- **`services/downloads.py` and `services/uploads.py`**: Cross-cutting helpers used by multiple resource services, not standalone command services.
- **Default timeout is 6 hours**: `DEFAULT_HTTP_TIMEOUT_SECONDS = 21600` in `core/timeouts.py` -- deliberately generous for large DICOM transfers. Uploads use a shorter 120s per-request timeout.
- **Auto-reauth on 401**: `XNATClient.auto_reauth` (default False) retries once with fresh credentials on `SessionExpiredError`. The `@require_auth` decorator also catches expired sessions and re-authenticates.
- **Retry logic**: Exponential backoff (2^attempt seconds) on 502/503/504 only. 401 triggers reauth, not retry. 400 is retryable in uploads only (transient XNAT import race).
- **Thread-local HTTP clients**: `uploads.py` uses `threading.local()` to give each worker its own httpx.Client, avoiding connection sharing across threads.
- **Quiet mode ID extraction**: Tries fields in order: `id` -> `ID` -> `label` -> `name`.
- **XNAT_URL creates a profile**: Setting `XNAT_URL` env var auto-creates/overrides the `default` profile at runtime.
- **`default_project` fallback on `-E`**: When `-P` is omitted, `session show/download` and all `scan` commands resolve `-P` from the profile's `default_project`. This means `-E SESSION_LABEL` works without `-P` if the profile has `default_project` set.
- **Version must be bumped in two places**: `pyproject.toml` and `xnatctl/__init__.py` (`__version__`) must match. To release a new version of this project: 1) Run all tests locally with pytest, 2) Bump the version in pyproject.toml (patch), 3) Update CHANGELOG.md, 4) Build the Docker image with `docker build -t registry/app:NEW_VERSION .`, 5) Run smoke tests against the built image: `docker run --rm registry/app:NEW_VERSION --help`, 6) If all passes, push the image and create a git tag. If any step fails, fix the issue and retry that step. Do not push until all checks pass. Show me the full log of each step.

## Versioning

Semantic Versioning (`MAJOR.MINOR.PATCH`). Source of truth: `project.version` in `pyproject.toml`. CI auto-tags on push to main.

| Bump | When | Examples |
|------|------|----------|
| `PATCH` | Bug fixes, refactors, docs, deps | Fix retry logic, fix table alignment |
| `MINOR` | New commands, flags, output fields | Add `scan download`, add `--parallel` flag |
| `MAJOR` | Breaking CLI surface, output schema, or config changes | Rename/remove command, change JSON shape |

Rules:
- Stay on `0.x` until CLI surface is stable until user clearly specifies otherwise
- Commit prefix: `feat()` -> MINOR, `fix()` -> PATCH, `breaking()` or `!` -> MAJOR
- **Never bump versions or create releases unless the user explicitly requests it**
