# xnatctl

A modern command-line interface for XNAT neuroimaging server administration.

## What is xnatctl?

xnatctl is a command-line tool for managing neuroimaging data on XNAT servers.
It lets you browse projects and subjects, download and upload imaging sessions,
run processing pipelines, and perform administrative tasks -- all from your
terminal. Whether you are a researcher downloading data for analysis or a system
administrator managing hundreds of subjects, xnatctl provides a consistent,
scriptable interface to your XNAT server.

## Features

- **Resource-centric commands** -- interact with XNAT the way you think about
  it: `xnatctl project list`, `xnatctl session download`, `xnatctl scan show`.
  Every command follows the pattern `xnatctl <resource> <action> [args]`.

- **Profile-based configuration** -- manage multiple XNAT servers with named
  profiles and switch between them instantly. Keep separate profiles for
  production, development, and collaboration servers in a single config file.

- **Consistent output formats** -- every command supports `--output json|table`
  and `--quiet` (IDs only), so you can pipe results into scripts or read them
  in a human-friendly table without changing your workflow.

- **Parallel operations** -- batch uploads and downloads run across multiple
  workers with real-time progress tracking. Large transfers stay fast without
  extra scripting.

- **Session authentication** -- log in once with `xnatctl auth login` and your
  session token is cached locally. Expired tokens are refreshed automatically,
  so you stay authenticated without repeated prompts.

- **Pure HTTP** -- xnatctl talks directly to the XNAT REST API using httpx.
  There is no dependency on pyxnat or any Java bridge, which keeps the install
  lightweight and the behavior predictable.

## Installation

### Standalone Binary (no Python required)

If you do not have Python installed or prefer a single executable, download a
pre-built binary. The install script auto-detects your OS and architecture:

```bash
# One-line install (latest release, auto-detects platform)
curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

# Install a specific version
XNATCTL_VERSION=v0.1.0 curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

# Custom install directory (default: ~/.local/bin)
XNATCTL_INSTALL_DIR=/usr/local/bin curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash
```

Or download manually from [GitHub Releases](https://github.com/rickyltwong/xnatctl/releases):

| Platform       | Asset                              |
|----------------|------------------------------------|
| Linux (x86_64) | `xnatctl-linux-amd64.tar.gz`      |
| macOS (x86_64) | `xnatctl-darwin-amd64.tar.gz`     |
| Windows (x86_64) | `xnatctl-windows-amd64.zip`     |

```bash
# Linux / macOS
tar -xzf xnatctl-<platform>-amd64.tar.gz
chmod +x xnatctl
mv xnatctl ~/.local/bin/

# Windows (PowerShell)
Expand-Archive xnatctl-windows-amd64.zip -DestinationPath .
Move-Item xnatctl.exe C:\Users\<you>\AppData\Local\bin\
```

### Python Package

If you already have Python 3.11+ and want to install xnatctl as a package,
use pip or uv:

```bash
# From PyPI (recommended)
pip install xnatctl

# With uv
uv pip install xnatctl

# For DICOM utilities (optional -- adds pydicom and pynetdicom)
pip install "xnatctl[dicom]"

# From source
pip install git+https://github.com/rickyltwong/xnatctl.git
```

### Docker

For containerized environments or CI pipelines where you want to avoid local
installation entirely:

```bash
docker run --rm ghcr.io/rickyltwong/xnatctl:main --help
```

For full installation instructions including shell completion and
troubleshooting, see the [Installation guide](docs/installation.rst).

## Quick Start

Once installed, you can be up and running in four steps.

**1. Create a configuration file.** This stores your XNAT server URL and
default settings so you do not have to pass them on every command:

```bash
xnatctl config init --url https://xnat.example.org
```

**2. Authenticate.** Log in with your XNAT credentials. The session token is
cached locally, so subsequent commands authenticate automatically:

```bash
xnatctl auth login
```

**3. Browse your data.** List the projects you have access to and inspect their
contents:

```bash
xnatctl project list
```

**4. Download a session.** Pull imaging data to your local machine for analysis.
Use an experiment accession number or, if you have a default project set, a
session label:

```bash
xnatctl session download XNAT_E00001 --out ./data
```

For a detailed walkthrough, see the [Quick Start guide](docs/quickstart.rst).

## Commands

| Command              | Description                                      |
|----------------------|--------------------------------------------------|
| `xnatctl config`    | Manage configuration profiles                    |
| `xnatctl auth`      | Authentication (login/logout/status)             |
| `xnatctl project`   | Project operations (list/show/create)            |
| `xnatctl subject`   | Subject operations (list/show/rename/delete)     |
| `xnatctl session`   | Session operations (list/show/download/upload)   |
| `xnatctl scan`      | Scan operations (list/show/delete/download)      |
| `xnatctl resource`  | Resource operations (list/upload/download)       |
| `xnatctl prearchive` | Prearchive management (list/archive/delete/move) |
| `xnatctl pipeline`  | Pipeline execution (list/run/status/cancel)      |
| `xnatctl admin`     | Administrative operations (users/catalogs/audit) |
| `xnatctl api`       | Raw API access (escape hatch for any endpoint)   |
| `xnatctl dicom`     | DICOM utilities (requires `xnatctl[dicom]`)      |

For complete usage and examples, see the [CLI Reference](docs/cli-reference.rst).

## Configuration

Config file location: `~/.config/xnatctl/config.yaml`

```yaml
default_profile: production
output_format: table

profiles:
  production:
    url: https://xnat.example.org
    username: myuser          # optional -- can also use env vars
    password: mypassword      # optional -- can also use env vars
    verify_ssl: true
    timeout: 30
    default_project: MYPROJECT

  development:
    url: https://xnat-dev.example.org
    verify_ssl: false
```

### Working with Profiles

Profiles let you store connection details for each XNAT server you work with.
You can create, add, and switch profiles from the command line:

```bash
# Create an initial config (prompts for URL and optional defaults)
xnatctl config init --url https://xnat.example.org

# Add additional profiles
xnatctl config add-profile dev --url https://xnat-dev.example.org --no-verify-ssl

# Switch the active profile
xnatctl config use-context dev

# Show the active profile and config
xnatctl config show
```

### Authentication Flow

Log in once and your session token is cached. xnatctl reuses the cached token
and refreshes it automatically when it expires:

```bash
# Login and cache a session token
xnatctl auth login

# Check current user and session context
xnatctl whoami
```

Credential priority (highest to lowest):

1. CLI arguments (`--username`, `--password`)
2. Environment variables (`XNAT_USER`, `XNAT_PASS`)
3. Profile config (`username`, `password` in config.yaml)
4. Interactive prompt

Session tokens are cached at `~/.config/xnatctl/.session` and used
automatically until they expire.

### Environment Variables

You can override any profile setting with environment variables. This is
especially useful for CI pipelines and non-interactive scripts:

| Variable        | Description                                          |
|-----------------|------------------------------------------------------|
| `XNAT_URL`     | Server URL                                           |
| `XNAT_USER`    | Username                                             |
| `XNAT_PASS`    | Password                                             |
| `XNAT_TOKEN`   | Session token (highest auth priority)                |
| `XNAT_PROFILE` | Config profile name                                  |

Notes:

- `XNAT_TOKEN` takes precedence over cached sessions and username/password
  credentials. Use it when you already have a valid token from another tool.
- `XNAT_URL` and `XNAT_PROFILE` override values from `config.yaml` for the
  current shell session.
- Use `XNAT_USER` and `XNAT_PASS` for non-interactive authentication in CI
  jobs and automated scripts.

## Documentation

Complete documentation is available in the [docs/](docs/) directory. Topics
include installation, key concepts, configuration, CLI reference, downloading,
uploading, workflows, and XNAT compatibility.

## Development

```bash
# Clone and install
git clone https://github.com/rickyltwong/xnatctl.git
cd xnatctl
uv sync --dev

# Run tests
uv run pytest tests/ -v

# Lint and format
uv run ruff check xnatctl scripts
uv run ruff format xnatctl scripts

# Type check
uv run mypy xnatctl
```

## License

MIT
