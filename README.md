# xnatctl

A modern CLI for XNAT neuroimaging server administration.

## Features

- **Resource-centric commands**: `xnatctl <resource> <action> [args]`
- **Profile-based configuration**: YAML config with multiple server profiles
- **Consistent output**: `--output json|table` and `--quiet` on all commands
- **Parallel operations**: Batch uploads/downloads with progress tracking
- **Session authentication**: Token caching with `auth login`
- **Pure HTTP**: Direct REST API calls with httpx (no pyxnat dependency)

## Installation

### Standalone Binary (no Python required)

Download a self-contained Linux binary -- no Python installation needed:

```bash
# One-line install (latest release)
curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

# Install a specific version
XNATCTL_VERSION=v0.1.0 curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

# Custom install directory (default: ~/.local/bin)
XNATCTL_INSTALL_DIR=/usr/local/bin curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash
```

Or download manually from [GitHub Releases](https://github.com/rickyltwong/xnatctl/releases):

```bash
# Download and extract
tar -xzf xnatctl-linux-amd64.tar.gz
chmod +x xnatctl
mv xnatctl ~/.local/bin/

# Verify
xnatctl --version
```

### Python Package

```bash
# With uv (recommended)
uv pip install git+https://github.com/rickyltwong/xnatctl.git

# With pip
pip install git+https://github.com/rickyltwong/xnatctl.git

# For DICOM utilities (optional)
pip install "xnatctl[dicom] @ git+https://github.com/rickyltwong/xnatctl.git"
```

### Docker

```bash
docker run --rm ghcr.io/rickyltwong/xnatctl:main --help
```

## Quick Start

```bash
# Create config file
xnatctl config init --url https://xnat.example.org

# Authenticate
xnatctl auth login

# List projects
xnatctl project list

# Download a session
xnatctl session download XNAT_E00001 --out ./data
```

## Commands

| Command | Description |
|---------|-------------|
| `xnatctl config` | Manage configuration profiles |
| `xnatctl auth` | Authentication (login/logout/status) |
| `xnatctl project` | Project operations (list/show/create) |
| `xnatctl subject` | Subject operations (list/show/rename/delete) |
| `xnatctl session` | Session operations (list/show/download/upload) |
| `xnatctl scan` | Scan operations (list/show/delete) |
| `xnatctl resource` | Resource operations (list/upload/download) |
| `xnatctl prearchive` | Prearchive management |
| `xnatctl pipeline` | Pipeline execution |
| `xnatctl admin` | Administrative operations |
| `xnatctl api` | Raw API access (escape hatch) |
| `xnatctl dicom` | DICOM utilities (requires pydicom) |

## Configuration

Config file location: `~/.config/xnatctl/config.yaml`

```yaml
default_profile: production
output_format: table

profiles:
  production:
    url: https://xnat.example.org
    username: myuser          # optional, can also use env vars
    password: mypassword      # optional, can also use env vars
    verify_ssl: true
    timeout: 30
    default_project: MYPROJECT

  development:
    url: https://xnat-dev.example.org
    verify_ssl: false
```

### Getting Started with Profiles

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

```bash
# Login and cache a session token
xnatctl auth login

# Check current user/session context
xnatctl whoami
```

Credential priority (highest to lowest):
1. CLI arguments (`--username`, `--password`)
2. Environment variables (`XNAT_USER`, `XNAT_PASS`)
3. Profile config (`username`, `password` in config.yaml)
4. Interactive prompt

Session tokens are cached under `~/.config/xnatctl/.session` and used automatically.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `XNAT_URL` | Server URL |
| `XNAT_USER` | Username |
| `XNAT_PASS` | Password |
| `XNAT_TOKEN` | Session token |
| `XNAT_PROFILE` | Config profile |

Notes:
- `XNAT_TOKEN` takes precedence over cached sessions and username/password.
- `XNAT_URL` and `XNAT_PROFILE` override values from `config.yaml` for the current shell.
- Use `XNAT_USER`/`XNAT_PASS` for non-interactive auth (CI, scripts).

## Development

```bash
# Clone and install
git clone https://github.com/rickyltwong/xnatctl.git
cd xnatctl
uv sync

# Run tests
uv run pytest

# Lint and format
uv run ruff check xnatctl
uv run ruff format xnatctl
```

## License

MIT
