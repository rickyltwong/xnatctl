# xnatctl Conversion Plan v0.1

> **Goal**: A single CLI that standardizes the XNAT REST workflows your team repeats
> (list → inspect → download/upload → trigger pipelines → audit)

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Current State Analysis](#current-state-analysis)
3. [Target Architecture](#target-architecture)
4. [Package Structure](#package-structure)
5. [Core Components](#core-components)
6. [Command Specifications](#command-specifications)
7. [Advanced Features](#advanced-features)
8. [Implementation Phases](#implementation-phases)

---

## Executive Summary

### What Changes

| Aspect | xnatio (Current) | xnatctl (Target) |
|--------|------------------|------------------|
| **Name** | `xnatio` / `xio` | `xnatctl` |
| **CLI Framework** | argparse (flat subparsers) | Click (nested command groups) |
| **Config** | `.env` files only | YAML profiles + env vars + token auth |
| **Output** | Inconsistent (text/json per command) | Unified `--output json|table` + `--quiet` |
| **Auth** | Per-request credentials | Session/token caching with `auth login` |
| **HTTP** | pyxnat wrapper | Pure httpx with retry/pagination |
| **Command Style** | `upload-dicom PROJECT SUBJECT SESSION` | `xnatctl session download <SESSION_ID>` |

### What We Absorb from xnatio

| Component | What We Keep | Adaptations |
|-----------|-------------|-------------|
| **Exception hierarchy** | All 20+ typed exceptions | Direct port to `core/exceptions.py` |
| **Validation module** | URL, port, ID, path, AE title validators | Direct port to `core/validation.py` |
| **Parallel upload** | ThreadPoolExecutor, batch splitting, progress callbacks | Adapt to httpx client |
| **Parallel download** | Concurrent downloads, progress reporting | Adapt to new service layer |
| **Session auth** | `XNATSession` class from parallel_rest.py | Integrate into core client |
| **Admin operations** | Catalog refresh, user groups, batch rename with merge | Port to admin service |
| **Audit logging** | `AuditLogger`, `LogContext` | Direct port |
| **Progress dataclasses** | `UploadProgress`, `UploadResult`, `UploadSummary` | Generalize for all operations |

---

## Current State Analysis

### Existing xnatio Commands → xnatctl Mapping

| xnatio Command | xnatctl Equivalent | Notes |
|----------------|-------------------|-------|
| `create-project` | `xnatctl project create` | Add `--description`, `--pi` |
| `delete-scans` | `xnatctl scan delete` | Keep `--parallel`, `--dry-run` |
| `list-scans` | `xnatctl scan list` | Unified output format |
| `rename-subjects` | `xnatctl subject rename --mapping` | Keep batch JSON support |
| `rename-subjects-pattern` | `xnatctl subject rename --pattern` | Keep merge support |
| `add-user-to-groups` | `xnatctl admin user add-to-groups` | Keep `--projects --role` |
| `download-session` | `xnatctl session download` | Add `--resume`, `--verify` |
| `extract-session` | `xnatctl local extract` | Local utility |
| `upload-dicom` | `xnatctl session upload` | Keep parallel REST + C-STORE |
| `upload-resource` | `xnatctl resource upload` | Keep zip+extract |
| `refresh-catalogs` | `xnatctl admin refresh-catalogs` | Keep parallel + options |
| `apply-label-fixes` | `xnatctl admin apply-fixes` | Site-specific, optional |

### Valuable Patterns to Preserve

**From `uploaders/parallel_rest.py`:**
```python
@dataclass
class UploadProgress:
    phase: str          # "archiving", "uploading", "complete", "error"
    current: int
    total: int
    message: str
    batch_id: int
    success: bool
    errors: List[str]

# Progress callback pattern
def upload_with_progress(callback: Callable[[UploadProgress], None]):
    ...
    callback(UploadProgress(phase="archiving", current=1, total=10, ...))
```

**From `core/validation.py`:**
```python
# Comprehensive validators - keep all of these
validate_server_url(url) -> str
validate_port(port) -> int
validate_project_id(project) -> str
validate_subject_id(subject) -> str
validate_session_id(session) -> str
validate_scan_id(scan_id) -> str
validate_ae_title(ae_title) -> str
validate_path_exists(path) -> Path
validate_path_writable(path) -> Path
validate_archive_path(path) -> Path
validate_workers(value) -> int
validate_timeout(value) -> int
validate_regex_pattern(pattern) -> re.Pattern
```

**From `services/admin.py`:**
```python
# Pattern-based rename with merge support
def rename_subjects_pattern(
    project: str,
    match_pattern: str,      # Regex with capture groups
    to_template: str,        # Template: "{project}_{1}"
    dry_run: bool = False,
) -> Dict[str, Any]:
    # Returns: renamed, merged, skipped
```

---

## Target Architecture

### Design Principles

1. **Resource-centric commands**: `xnatctl <resource> <action> [args]`
2. **Consistent output contract**: All commands support `--output json|table` and `--quiet`
3. **Ops safety**: `--dry-run` everywhere, confirmations for destructive ops
4. **Profile-based config**: Switch between environments with `--profile`
5. **Pure HTTP**: No pyxnat dependency, direct REST API calls with httpx
6. **Parallel by default**: Batch operations use ThreadPoolExecutor
7. **Progress feedback**: Rich progress bars for long operations

### CLI Command Tree (Extended)

```
xnatctl
├── config
│   ├── init                    # Create config file
│   ├── show                    # Show current config
│   ├── use-context <PROFILE>   # Switch active profile
│   └── current-context         # Show active profile
├── auth
│   ├── login                   # Authenticate and cache session
│   └── logout                  # Clear cached session
├── whoami                      # Show current user context
├── health
│   └── ping                    # Connectivity check
├── api                         # Raw API access (escape hatch)
│   ├── get <PATH>              # GET any endpoint
│   ├── post <PATH>             # POST any endpoint
│   ├── put <PATH>              # PUT any endpoint
│   └── delete <PATH>           # DELETE any endpoint
├── project
│   ├── list                    # List accessible projects
│   ├── show <PROJECT>          # Show project details
│   └── create <PROJECT>        # Create project
├── subject
│   ├── list                    # List subjects
│   ├── show <SUBJECT>          # Show subject details
│   ├── rename                  # Rename subjects (batch/pattern)
│   └── delete <SUBJECT>        # Delete subject
├── session
│   ├── list                    # List sessions
│   ├── show <SESSION>          # Show session details
│   ├── download <SESSION>      # Download session data
│   └── upload                  # Upload DICOM session
├── scan
│   ├── list <SESSION>          # List scans
│   └── delete                  # Delete scans
├── resource
│   ├── list <SESSION>          # List resources
│   ├── upload                  # Upload resource
│   └── download                # Download resource
├── prearchive                  # Prearchive management
│   ├── list                    # List prearchive sessions
│   ├── archive <SESSION>       # Archive session
│   └── delete <SESSION>        # Delete from prearchive
├── pipeline
│   ├── list                    # List available pipelines
│   ├── run <PIPELINE>          # Trigger pipeline
│   └── status <JOB_ID>         # Check job status
├── admin
│   ├── refresh-catalogs        # Refresh catalog XMLs
│   ├── user
│   │   └── add-to-groups       # Add user to groups
│   └── audit                   # View audit log
├── dicom                       # DICOM utilities (optional)
│   ├── validate <PATH>         # Validate DICOM files
│   └── inspect <FILE>          # Show DICOM headers
└── completion                  # Shell completions
    ├── bash                    # Generate bash completion
    ├── zsh                     # Generate zsh completion
    └── fish                    # Generate fish completion
```

---

## Package Structure

### Directory Layout

```
xnatctl/
├── __init__.py               # Package exports, __version__
├── __main__.py               # Allow `python -m xnatctl`
├── cli/
│   ├── __init__.py           # Click app factory
│   ├── main.py               # Root CLI group + global options
│   ├── common.py             # Shared decorators, output helpers
│   ├── auth.py               # auth login/logout
│   ├── config_cmd.py         # config init/show/use-context
│   ├── project.py            # project list/show/create
│   ├── subject.py            # subject list/show/rename/delete
│   ├── session.py            # session list/show/download/upload
│   ├── scan.py               # scan list/delete
│   ├── resource.py           # resource list/upload/download
│   ├── prearchive.py         # prearchive list/archive/delete
│   ├── pipeline.py           # pipeline list/run/status
│   ├── admin.py              # admin refresh-catalogs/user/audit
│   ├── api.py                # api get/post/put/delete
│   └── dicom_cmd.py          # dicom validate/inspect (optional)
├── core/
│   ├── __init__.py           # Core exports
│   ├── client.py             # XNATClient (httpx, retry, pagination)
│   ├── config.py             # Config loading (YAML profiles + env)
│   ├── auth.py               # Token/session management + keyring
│   ├── exceptions.py         # Exception hierarchy (from xnatio)
│   ├── validation.py         # Input validators (from xnatio)
│   ├── logging.py            # Logging utilities (from xnatio)
│   └── output.py             # Output formatters (JSON, table, quiet)
├── models/
│   ├── __init__.py
│   ├── base.py               # Base model with common fields
│   ├── project.py            # Project model
│   ├── subject.py            # Subject model
│   ├── session.py            # Session/Experiment model
│   ├── scan.py               # Scan model
│   ├── resource.py           # Resource model
│   └── progress.py           # UploadProgress, DownloadProgress, etc.
├── services/
│   ├── __init__.py           # Service exports
│   ├── base.py               # BaseService with common methods
│   ├── projects.py           # ProjectService
│   ├── subjects.py           # SubjectService
│   ├── sessions.py           # SessionService
│   ├── scans.py              # ScanService
│   ├── resources.py          # ResourceService
│   ├── downloads.py          # DownloadService (parallel, resume)
│   ├── uploads.py            # UploadService (parallel REST, C-STORE)
│   ├── prearchive.py         # PrearchiveService
│   ├── pipelines.py          # PipelineService
│   └── admin.py              # AdminService (catalog, users, audit)
└── py.typed                  # PEP 561 marker
```

### Dependencies

```toml
[project]
name = "xnatctl"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "click>=8.1.0",           # CLI framework
    "httpx>=0.27.0",          # Modern HTTP client
    "pyyaml>=6.0",            # Config files
    "rich>=13.0",             # Tables, progress bars, syntax highlighting
    "keyring>=25.0",          # Secure credential storage
    "python-dotenv>=1.0",     # Env file support (legacy compat)
    "pydantic>=2.0",          # Data validation and models
]

[project.optional-dependencies]
dicom = [
    "pydicom>=2.4.4",         # DICOM parsing
    "pynetdicom>=2.0.2",      # DICOM C-STORE
]
dev = [
    "pytest>=8.0",
    "pytest-cov>=4.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
    "mypy>=1.10",
]

[project.scripts]
xnatctl = "xnatctl.cli:main"
```

---

## Core Components

### 1. HTTP Client (`core/client.py`)

```python
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Callable
import httpx
from .exceptions import (
    AuthenticationError,
    NetworkError,
    RetryExhaustedError,
    ServerUnreachableError,
)

@dataclass
class XNATClient:
    """Pure HTTP client with retry, pagination, and session auth."""

    base_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    session_token: Optional[str] = None
    timeout: int = 30
    max_retries: int = 3
    verify_ssl: bool = True
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self):
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )

    # =========================================================================
    # Authentication
    # =========================================================================

    def authenticate(self) -> str:
        """Authenticate and return JSESSIONID."""
        resp = self._client.post(
            "/data/JSESSION",
            auth=(self.username, self.password),
        )
        if resp.status_code != 200 or "<html" in resp.text.lower():
            raise AuthenticationError(self.base_url, "Invalid credentials")
        self.session_token = resp.text.strip()
        return self.session_token

    def invalidate_session(self) -> None:
        """Logout and clear session."""
        if self.session_token:
            try:
                self._client.delete(
                    "/data/JSESSION",
                    cookies={"JSESSIONID": self.session_token},
                )
            except Exception:
                pass
        self.session_token = None

    @property
    def is_authenticated(self) -> bool:
        return self.session_token is not None

    # =========================================================================
    # HTTP Methods with Retry
    # =========================================================================

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """Execute request with retry logic and auth."""
        cookies = {}
        if self.session_token:
            cookies["JSESSIONID"] = self.session_token

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(
                    method,
                    path,
                    cookies=cookies,
                    **kwargs,
                )
                resp.raise_for_status()
                return resp
            except httpx.TimeoutException as e:
                last_error = e
            except httpx.ConnectError as e:
                last_error = e
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise AuthenticationError(self.base_url)
                raise

            if attempt < self.max_retries:
                import time
                time.sleep(2 ** (attempt + 1))

        raise RetryExhaustedError("request", self.max_retries + 1, last_error)

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> httpx.Response:
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self._request("DELETE", path, **kwargs)

    # =========================================================================
    # Pagination
    # =========================================================================

    def paginate(
        self,
        path: str,
        page_size: int = 100,
        result_key: str = "ResultSet.Result",
    ) -> Iterator[dict]:
        """Paginated GET returning items one by one."""
        offset = 0
        while True:
            resp = self.get(
                path,
                params={"offset": offset, "limit": page_size, "format": "json"},
            )
            data = resp.json()

            # Navigate to results using dot notation
            results = data
            for key in result_key.split("."):
                results = results.get(key, [])

            if not results:
                break

            yield from results
            offset += page_size

            if len(results) < page_size:
                break

    # =========================================================================
    # Context Manager
    # =========================================================================

    def __enter__(self) -> "XNATClient":
        return self

    def __exit__(self, *args) -> None:
        self._client.close()
```

### 2. Configuration (`core/config.py`)

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any
import yaml
import os

CONFIG_DIR = Path.home() / ".config" / "xnatctl"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
SESSION_CACHE = CONFIG_DIR / ".sessions"

@dataclass
class Profile:
    """Single XNAT server profile."""
    url: str
    verify_ssl: bool = True
    timeout: int = 30
    default_project: Optional[str] = None

@dataclass
class Config:
    """Application configuration."""
    default_profile: str = "default"
    output_format: str = "table"
    profiles: Dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load config from file, with env var overrides."""
        path = config_path or CONFIG_FILE

        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        profiles = {}
        for name, pdata in data.get("profiles", {}).items():
            profiles[name] = Profile(**pdata)

        config = cls(
            default_profile=data.get("default_profile", "default"),
            output_format=data.get("output_format", "table"),
            profiles=profiles,
        )

        # Env var overrides
        if url := os.getenv("XNAT_URL"):
            config.profiles["default"] = Profile(
                url=url,
                verify_ssl=os.getenv("XNAT_VERIFY_SSL", "true").lower() == "true",
            )

        return config

    def save(self, config_path: Optional[Path] = None) -> None:
        """Save config to file (excludes secrets)."""
        path = config_path or CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "default_profile": self.default_profile,
            "output_format": self.output_format,
            "profiles": {
                name: {
                    "url": p.url,
                    "verify_ssl": p.verify_ssl,
                    "timeout": p.timeout,
                    "default_project": p.default_project,
                }
                for name, p in self.profiles.items()
            },
        }

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    def get_profile(self, name: Optional[str] = None) -> Profile:
        """Get profile by name or default."""
        name = name or self.default_profile
        if name not in self.profiles:
            raise ValueError(f"Profile not found: {name}")
        return self.profiles[name]
```

**Config file format (`~/.config/xnatctl/config.yaml`):**

```yaml
default_profile: production
output_format: table

profiles:
  production:
    url: https://xnat.example.org
    verify_ssl: true
    timeout: 30
    default_project: MYPROJECT

  development:
    url: https://xnat-dev.example.org
    verify_ssl: false
    timeout: 60
```

### 3. Output Formatters (`core/output.py`)

```python
from enum import Enum
from typing import Any, Sequence, Optional
from rich.console import Console
from rich.table import Table
import json
import sys

console = Console()
err_console = Console(stderr=True)

class OutputFormat(Enum):
    JSON = "json"
    TABLE = "table"

def print_table(
    rows: Sequence[dict],
    columns: Sequence[str],
    title: Optional[str] = None,
) -> None:
    """Print rich table to stdout."""
    table = Table(title=title, show_header=True, header_style="bold")

    for col in columns:
        table.add_column(col)

    for row in rows:
        table.add_row(*[str(row.get(col, "")) for col in columns])

    console.print(table)

def print_json(data: Any, indent: int = 2) -> None:
    """Print JSON to stdout."""
    print(json.dumps(data, indent=indent, default=str))

def print_output(
    data: Any,
    format: OutputFormat,
    columns: Optional[Sequence[str]] = None,
    quiet: bool = False,
) -> None:
    """Print data in specified format."""
    if quiet:
        # Quiet mode: just IDs, one per line
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    print(item.get("id") or item.get("ID") or item.get("label", ""))
                else:
                    print(item)
        return

    if format == OutputFormat.JSON:
        print_json(data)
    elif format == OutputFormat.TABLE and columns:
        if isinstance(data, list):
            print_table(data, columns)
        elif isinstance(data, dict):
            print_table([data], columns)
    else:
        print_json(data)

def print_error(message: str) -> None:
    """Print error message to stderr."""
    err_console.print(f"[red]Error:[/red] {message}")

def print_warning(message: str) -> None:
    """Print warning to stderr."""
    err_console.print(f"[yellow]Warning:[/yellow] {message}")

def print_success(message: str) -> None:
    """Print success message."""
    console.print(f"[green]✓[/green] {message}")
```

### 4. CLI Common Decorators (`cli/common.py`)

```python
import click
from functools import wraps
from typing import Optional
from ..core.config import Config
from ..core.client import XNATClient
from ..core.output import OutputFormat, print_error

pass_client = click.make_pass_decorator(XNATClient)

def global_options(f):
    """Add global options to command."""
    @click.option('--profile', '-p', envvar='XNAT_PROFILE',
                  help='Config profile to use')
    @click.option('--output', '-o', 'output_format',
                  type=click.Choice(['json', 'table']),
                  default='table', help='Output format')
    @click.option('--quiet', '-q', is_flag=True,
                  help='Minimal output (IDs only)')
    @wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

def require_auth(f):
    """Ensure user is authenticated."""
    @wraps(f)
    @pass_client
    def wrapper(client: XNATClient, *args, **kwargs):
        if not client.is_authenticated:
            raise click.ClickException(
                "Not authenticated. Run 'xnatctl auth login' first."
            )
        return f(client, *args, **kwargs)
    return wrapper

def confirm_destructive(message: str):
    """Require confirmation for destructive operations."""
    def decorator(f):
        @click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
        @click.option('--dry-run', is_flag=True, help='Preview without changes')
        @wraps(f)
        def wrapper(*args, yes, dry_run, **kwargs):
            if dry_run:
                click.echo("[DRY-RUN] Preview mode - no changes will be made", err=True)
            elif not yes:
                click.confirm(message, abort=True)
            return f(*args, dry_run=dry_run, **kwargs)
        return wrapper
    return decorator

def batch_option(f):
    """Add --batch option for bulk operations."""
    @click.option('--batch', type=click.Path(exists=True),
                  help='File with IDs (one per line) or JSON array')
    @wraps(f)
    def wrapper(*args, batch, **kwargs):
        if batch:
            with open(batch) as f:
                content = f.read().strip()
                if content.startswith('['):
                    import json
                    kwargs['ids'] = json.loads(content)
                else:
                    kwargs['ids'] = [line.strip() for line in content.splitlines() if line.strip()]
        return f(*args, **kwargs)
    return wrapper

def parallel_options(f):
    """Add parallel execution options."""
    @click.option('--parallel/--no-parallel', default=True,
                  help='Enable parallel execution')
    @click.option('--workers', type=int, default=4,
                  help='Max parallel workers')
    @wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper
```

### 5. Progress Models (`models/progress.py`)

```python
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

class OperationPhase(Enum):
    PREPARING = "preparing"
    ARCHIVING = "archiving"
    UPLOADING = "uploading"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETE = "complete"
    ERROR = "error"

@dataclass
class Progress:
    """Base progress information."""
    phase: OperationPhase
    current: int = 0
    total: int = 0
    message: str = ""
    success: bool = True
    errors: List[str] = field(default_factory=list)

@dataclass
class UploadProgress(Progress):
    """Upload-specific progress."""
    batch_id: int = 0
    bytes_sent: int = 0
    total_bytes: int = 0

@dataclass
class DownloadProgress(Progress):
    """Download-specific progress."""
    bytes_received: int = 0
    total_bytes: int = 0
    file_path: str = ""

@dataclass
class OperationResult:
    """Generic operation result."""
    success: bool
    total: int
    succeeded: int
    failed: int
    duration: float
    errors: List[str] = field(default_factory=list)

@dataclass
class UploadSummary(OperationResult):
    """Upload operation summary."""
    total_files: int = 0
    total_size_mb: float = 0.0
    batches_succeeded: int = 0
    batches_failed: int = 0

@dataclass
class DownloadSummary(OperationResult):
    """Download operation summary."""
    total_files: int = 0
    total_size_mb: float = 0.0
    output_path: str = ""
```

---

## Command Specifications

### Core Commands (Phase 1)

#### `xnatctl config init`
```
Create ~/.config/xnatctl/config.yaml with defaults.

Usage: xnatctl config init [OPTIONS]

Options:
  --url TEXT        XNAT server URL (prompted if not provided)
  --profile TEXT    Profile name (default: "default")
  --force           Overwrite existing config

Exit codes: 0=created, 1=exists (use --force)
```

#### `xnatctl config use-context <PROFILE>`
```
Switch active profile.

Usage: xnatctl config use-context PROFILE

Exit codes: 0=switched, 1=profile not found
```

#### `xnatctl auth login`
```
Authenticate and store session securely.

Usage: xnatctl auth login [OPTIONS]

Options:
  --username TEXT   XNAT username (or XNAT_USER env)
  --password TEXT   XNAT password (prompted if not provided)
  --token TEXT      Use existing session token

Exit codes: 0=authenticated, 1=failed, 2=server unreachable
```

#### `xnatctl auth logout`
```
Clear cached session/token.

Usage: xnatctl auth logout

Exit codes: 0=logged out
```

#### `xnatctl whoami`
```
Show current user, auth mode, and context.

Usage: xnatctl whoami [OPTIONS]

Options:
  -o, --output [json|table]

Output:
  User:     admin
  Server:   https://xnat.example.org
  Profile:  production
  Project:  MYPROJECT (default)

Exit codes: 0=success, 1=not authenticated
```

#### `xnatctl health ping`
```
Connectivity and auth check.

Usage: xnatctl health ping [OPTIONS]

Options:
  -o, --output [json|table]

Output:
  Status:   OK
  Server:   https://xnat.example.org
  Version:  1.8.5
  Latency:  45ms

Exit codes: 0=ok, 1=connection failed, 2=auth failed
```

### Resource Commands (Phase 2)

#### `xnatctl project list`
```
List accessible projects.

Usage: xnatctl project list [OPTIONS]

Options:
  -o, --output [json|table]
  -q, --quiet              Only output project IDs
  --limit INT              Max results

Output columns: ID, Name, PI, Subjects, Sessions

Exit codes: 0=success, 1=error
```

#### `xnatctl project show <PROJECT>`
```
Show project details.

Usage: xnatctl project show [OPTIONS] PROJECT

Options:
  -o, --output [json|table]

Output: ID, Name, PI, Description, Accessibility, Subject/Session counts

Exit codes: 0=success, 1=not found
```

#### `xnatctl subject list`
```
List subjects in a project.

Usage: xnatctl subject list [OPTIONS]

Options:
  --project TEXT           Project ID (required or from config)
  --filter TEXT            Filter expression
  -o, --output [json|table]
  -q, --quiet

Output columns: ID, Label, Sessions

Exit codes: 0=success, 1=project not found
```

#### `xnatctl session list`
```
List experiments/sessions.

Usage: xnatctl session list [OPTIONS]

Options:
  --project TEXT           Project ID
  --subject TEXT           Filter by subject
  --modality [MR|PET|CT]   Filter by modality
  -o, --output [json|table]
  -q, --quiet

Output columns: ID, Label, Subject, Date, Modality, Scans

Exit codes: 0=success, 1=error
```

#### `xnatctl session show <SESSION>`
```
Show session details.

Usage: xnatctl session show [OPTIONS] SESSION

Options:
  -o, --output [json|table]

Output sections:
  - Session info
  - Scans table
  - Resources table

Exit codes: 0=success, 1=not found
```

#### `xnatctl session download <SESSION>`
```
Download session data with resume support.

Usage: xnatctl session download [OPTIONS] SESSION

Options:
  --out, -o PATH           Output directory (required)
  --include-resources      Include session-level resources
  --include-assessors      Include assessor data
  --pattern TEXT           File pattern filter
  --resume                 Resume interrupted download
  --verify                 Verify checksums after download
  --dry-run                Preview what would be downloaded
  --parallel/--no-parallel Enable parallel downloads
  --workers INT            Max parallel workers
  -q, --quiet

Exit codes: 0=complete, 1=not found, 2=download failed
```

#### `xnatctl scan list <SESSION>`
```
List scans within a session.

Usage: xnatctl scan list [OPTIONS] SESSION

Options:
  -o, --output [json|table]
  -q, --quiet

Output columns: ID, Type, Series Description, Quality, Frames

Exit codes: 0=success, 1=session not found
```

#### `xnatctl resource list <SESSION>`
```
List resources at session or scan level.

Usage: xnatctl resource list [OPTIONS] SESSION

Options:
  --scan TEXT              Scope to specific scan
  -o, --output [json|table]

Output columns: Label, File Count, Size, URI

Exit codes: 0=success, 1=not found
```

### Admin Commands (Phase 3)

#### `xnatctl admin refresh-catalogs`
```
Refresh catalog XMLs for project experiments.

Usage: xnatctl admin refresh-catalogs [OPTIONS] PROJECT

Options:
  --option [checksum|delete|append|populateStats]  (repeatable)
  --experiment TEXT        Specific experiment IDs (repeatable)
  --limit INT              Limit experiments
  --parallel/--no-parallel
  --workers INT
  -o, --output [json|table]

Exit codes: 0=success, 1=error
```

#### `xnatctl admin user add-to-groups`
```
Add user to XNAT groups.

Usage: xnatctl admin user add-to-groups [OPTIONS] USERNAME

Options:
  GROUPS...                Group names
  --projects TEXT          Comma-separated project IDs
  --role TEXT              Role (default: member)

Exit codes: 0=success, 1=partial failure
```

#### `xnatctl admin audit`
```
View audit log.

Usage: xnatctl admin audit [OPTIONS]

Options:
  --project TEXT           Filter by project
  --user TEXT              Filter by user
  --action TEXT            Filter by action type
  --since TEXT             Time range (e.g., "7d", "2024-01-01")
  --limit INT              Max results
  -o, --output [json|table]

Exit codes: 0=success
```

### Raw API Access (Escape Hatch)

#### `xnatctl api get <PATH>`
```
Direct GET to any XNAT endpoint.

Usage: xnatctl api get [OPTIONS] PATH

Options:
  --params TEXT            Query params as key=value (repeatable)
  -o, --output [json|table]

Example:
  xnatctl api get /data/projects/MYPROJ/subjects --params columns=ID,label
```

#### `xnatctl api post <PATH>`
```
Direct POST to any XNAT endpoint.

Usage: xnatctl api post [OPTIONS] PATH

Options:
  --data TEXT              Request body
  --file PATH              Read body from file
  --params TEXT            Query params

Example:
  xnatctl api post /data/services/import --file payload.json
```

---

## Advanced Features

### 1. Batch Operations

```bash
# Download multiple sessions from a file
xnatctl session download --batch sessions.txt --out ./data

# sessions.txt:
# XNAT_E00001
# XNAT_E00002
# XNAT_E00003

# Bulk delete scans
xnatctl scan delete --batch scan-list.json --yes

# scan-list.json:
# [{"session": "XNAT_E00001", "scans": ["1", "2"]},
#  {"session": "XNAT_E00002", "scans": ["*"]}]
```

### 2. Pattern-Based Subject Rename with Merge

```bash
# Rename subjects matching pattern
xnatctl subject rename --project MYPROJ \
  --pattern "^(\w+)_session(\d+)$" \
  --to "{1}" \
  --dry-run

# This merges subjects like "SUB001_session1" and "SUB001_session2" into "SUB001"
```

### 3. Watch/Wait for Async Operations

```bash
# Trigger pipeline and wait for completion
xnatctl pipeline run PIPELINE --session X --wait --timeout 3600

# Poll job status
xnatctl pipeline status JOB_ID --watch --interval 30
```

### 4. Prearchive Management

```bash
# List prearchive sessions
xnatctl prearchive list --project MYPROJ

# Archive session from prearchive
xnatctl prearchive archive PREARCHIVE_SESSION_ID

# Delete from prearchive
xnatctl prearchive delete PREARCHIVE_SESSION_ID --yes
```

### 5. Shell Completion

```bash
# Generate and install completions
xnatctl completion bash > ~/.local/share/bash-completion/completions/xnatctl
xnatctl completion zsh > ~/.zfunc/_xnatctl
xnatctl completion fish > ~/.config/fish/completions/xnatctl.fish
```

### 6. Resume Interrupted Downloads

```bash
# Download with resume support
xnatctl session download SESSION --out ./data --resume

# If interrupted, re-run same command to continue
```

### 7. Checksum Verification

```bash
# Verify download integrity
xnatctl session download SESSION --out ./data --verify
```

---

## Exit Code Contract

All commands follow consistent exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (resource not found, validation failed) |
| 2 | Authentication error |
| 3 | Network/connection error |
| 4 | Permission denied |
| 5 | User cancelled (Ctrl+C, declined confirmation) |

---

## Implementation Phases

### Phase 1: Core Infrastructure (Week 1-2)

**Deliverables:**
- [ ] Package skeleton with Click CLI
- [ ] HTTP client with retry, auth, pagination
- [ ] Config system (YAML profiles + env)
- [ ] Auth management (login/logout/keyring)
- [ ] Output formatters (JSON/table/quiet)
- [ ] Exception hierarchy (port from xnatio)
- [ ] Validation module (port from xnatio)
- [ ] Shell completion support

**Commands:**
- `xnatctl config init/show/use-context`
- `xnatctl auth login/logout`
- `xnatctl whoami`
- `xnatctl health ping`
- `xnatctl completion bash/zsh/fish`

### Phase 2: Read Operations (Week 3-4)

**Deliverables:**
- [ ] Project service
- [ ] Subject service
- [ ] Session service
- [ ] Scan service
- [ ] Resource service

**Commands:**
- `xnatctl project list/show/create`
- `xnatctl subject list/show`
- `xnatctl session list/show`
- `xnatctl scan list`
- `xnatctl resource list`
- `xnatctl api get/post/put/delete`

### Phase 3: Write Operations (Week 5-6)

**Deliverables:**
- [ ] Download service (parallel, resume, verify)
- [ ] Upload service (parallel REST, progress)
- [ ] Prearchive service
- [ ] Progress tracking with Rich

**Commands:**
- `xnatctl session download`
- `xnatctl session upload`
- `xnatctl resource upload`
- `xnatctl prearchive list/archive/delete`
- `xnatctl scan delete`
- `xnatctl subject delete`

### Phase 4: Admin & Advanced (Week 7-8)

**Deliverables:**
- [ ] Admin service (catalog, users, audit)
- [ ] Pipeline service
- [ ] Batch operations
- [ ] Watch/wait functionality
- [ ] Subject rename (batch + pattern + merge)

**Commands:**
- `xnatctl admin refresh-catalogs`
- `xnatctl admin user add-to-groups`
- `xnatctl admin audit`
- `xnatctl subject rename`
- `xnatctl pipeline list/run/status`

### Phase 5: Polish & Optional (Week 9+)

**Deliverables:**
- [ ] DICOM utilities (optional, requires pydicom)
- [ ] Comprehensive tests
- [ ] Documentation
- [ ] CI/CD setup
- [ ] PyPI release

**Commands:**
- `xnatctl dicom validate`
- `xnatctl dicom inspect`

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `XNAT_URL` | Server URL | - |
| `XNAT_USER` | Username | - |
| `XNAT_PASS` | Password | - |
| `XNAT_TOKEN` | Session token | - |
| `XNAT_PROFILE` | Config profile | `default` |
| `XNAT_VERIFY_SSL` | Verify TLS | `true` |
| `XNAT_TIMEOUT` | Request timeout | `30` |

---

## Next Steps

1. **Review this plan** - Confirm command structure and priorities
2. **Create package skeleton** - Set up xnatctl directory structure
3. **Port core modules** - Exceptions, validation, logging from xnatio
4. **Implement Phase 1** - Core infrastructure + auth commands
5. **Iterate** - Build out phases incrementally

---

*Document version: 0.2.0*
*Last updated: 2026-01-16*
