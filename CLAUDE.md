# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

xnatctl is a modern CLI for XNAT neuroimaging server administration. It provides resource-centric commands with consistent output formats, parallel operations, and profile-based configuration.

## Directory Structure

```
xnatctl/
├── __init__.py               # Package exports, __version__
├── __main__.py               # Allow `python -m xnatctl`
├── py.typed                  # PEP 561 marker
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
│   ├── auth.py               # Token/session management
│   ├── exceptions.py         # Exception hierarchy
│   ├── validation.py         # Input validators
│   ├── logging.py            # Logging utilities
│   └── output.py             # Output formatters (JSON, table, quiet)
├── models/
│   ├── __init__.py
│   ├── base.py               # BaseModel, XNATResource (Pydantic)
│   ├── project.py            # Project model
│   ├── subject.py            # Subject model
│   ├── session.py            # Session/Experiment model
│   ├── scan.py               # Scan model
│   ├── resource.py           # Resource model
│   └── progress.py           # UploadProgress, DownloadProgress
└── services/
    ├── __init__.py           # Service exports
    ├── base.py               # BaseService with common methods
    ├── projects.py           # ProjectService
    ├── subjects.py           # SubjectService
    ├── sessions.py           # SessionService
    ├── scans.py              # ScanService
    ├── resources.py          # ResourceService
    ├── downloads.py          # DownloadService (parallel, resume)
    ├── uploads.py            # UploadService (parallel REST)
    ├── prearchive.py         # PrearchiveService
    ├── pipelines.py          # PipelineService
    └── admin.py              # AdminService
```

## Development Commands

```bash
# Install for development
uv sync

# Run CLI
uv run xnatctl --help

# Run tests
uv run pytest tests/ -v

# Lint and format
uv run ruff check xnatctl
uv run ruff format xnatctl

# Type check
uv run mypy xnatctl
```

## Architecture

### Service Layer Pattern
Commands use service classes that encapsulate XNAT REST API operations:

```python
from xnatctl.core.client import XNATClient
from xnatctl.services.projects import ProjectService

client = XNATClient(base_url="https://xnat.example.org", ...)
client.authenticate()

service = ProjectService(client)
projects = service.list()
```

### CLI Structure
CLI commands follow Click patterns with nested groups:

```python
@click.group()
def project():
    """Manage XNAT projects."""
    pass

@project.command("list")
@click.option("--output", "-o", type=click.Choice(["json", "table"]))
def project_list(output):
    ...
```

## Key Design Principles

1. **Resource-centric**: `xnatctl <resource> <action> [args]`
2. **Consistent output**: All commands support `--output json|table` and `--quiet`
3. **Ops safety**: `--dry-run` for destructive operations, confirmations
4. **Profile-based config**: Switch environments with `--profile`
5. **Pure HTTP**: Direct REST API calls with httpx, no pyxnat
6. **Parallel by default**: Batch operations use ThreadPoolExecutor

## Coding Style

- Python 3.10+; type hints throughout
- Click for CLI framework
- Pydantic for data models
- httpx for HTTP client
- Rich for output formatting
- Use `ruff` for linting/formatting

## Configuration

Config file: `~/.config/xnatctl/config.yaml`

Environment variables:
- `XNAT_URL`, `XNAT_USER`, `XNAT_PASS` - Server credentials
- `XNAT_TOKEN` - Session token
- `XNAT_PROFILE` - Active profile name

## Commit Guidelines

- Short, descriptive messages with scope prefix
- Examples: `feat(cli):`, `fix(client):`, `docs:`, `refactor(services):`
