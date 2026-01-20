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

```bash
# With uv (recommended)
uv pip install xnatctl

# With pip
pip install xnatctl

# For DICOM utilities (optional)
pip install xnatctl[dicom]
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
    verify_ssl: true
    timeout: 30
    default_project: MYPROJECT

  development:
    url: https://xnat-dev.example.org
    verify_ssl: false
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `XNAT_URL` | Server URL |
| `XNAT_USER` | Username |
| `XNAT_PASS` | Password |
| `XNAT_TOKEN` | Session token |
| `XNAT_PROFILE` | Config profile |

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
