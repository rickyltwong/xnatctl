# xnatctl

A modern command-line interface for [XNAT](https://xnat.org/) neuroimaging
server administration.

xnatctl lets you browse projects and subjects, download and upload imaging
sessions, run processing pipelines, and perform administrative tasks -- all
from your terminal, in the resource-centric style of `kubectl`:
`xnatctl <resource> <action> [args]`.

## Get Started

### 1. Install

Pick one:

```bash
# Standalone binary, no Python required (Linux/macOS, auto-detects platform)
curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

# Python package (needs Python 3.11+)
pip install xnatctl
```

A Docker image is also published (`ghcr.io/rickyltwong/xnatctl:main`) for CI
pipelines and containerized environments. Windows binaries, manual downloads,
Docker usage, shell completion, and troubleshooting are covered in the
[Installation guide](docs/installation.rst).

### 2. Connect to your server

```bash
xnatctl config init --url https://xnat.example.org   # create a config profile
xnatctl auth login                                   # log in; the session token is cached
xnatctl project list                                 # verify: list projects you can access
```

### 3. Download a DICOM session

Find a session, then pull its imaging data to your machine:

```bash
# List the sessions in a project
xnatctl session list -P MYPROJECT

# Download one session by accession number (or by label, with -P)
xnatctl session download -E XNAT_E00001 --out ./data --extract
```

With `--extract` you get one directory per scan with the DICOM files ready
for analysis; without it, the downloaded ZIP archives are kept as-is.

### 4. Download a session-level resource

Sessions can carry non-DICOM resources (behavioral data, physio logs,
derived outputs). List what a session has, then fetch one by label:

```bash
# See which resources the session carries
xnatctl resource list XNAT_E00001

# Download one resource as a ZIP
xnatctl resource download XNAT_E00001 LINKED_DATA -f linked_data.zip
```

That is the core loop. The [Quick Start guide](docs/quickstart.rst) continues
from here with uploads, batch operations, and scripting patterns.

## Commands

```text
xnatctl config      Manage configuration profiles
xnatctl auth        Authentication (login/logout/status)
xnatctl project     Project operations (list/show/create/transfer)
xnatctl subject     Subject operations (list/show/rename/delete)
xnatctl session     Session operations (list/show/download/upload)
xnatctl scan        Scan operations (list/show/delete/download)
xnatctl resource    Resource operations (list/upload/download)
xnatctl prearchive  Prearchive management (list/archive/delete/move)
xnatctl pipeline    Pipeline execution (list/run/status/cancel)
xnatctl xsync       Cross-server sync (sync/status/history)
xnatctl admin       Administrative operations (users/catalogs/audit)
xnatctl api         Raw API access (escape hatch for any endpoint)
xnatctl local       Offline operations (extract downloaded ZIPs)
xnatctl dicom       DICOM utilities (requires xnatctl[dicom])
```

Resource-oriented commands support `--output json|table` and `--quiet` (IDs
only), so the same command works interactively and in scripts. Full usage and
examples are in the [CLI Reference](docs/cli-reference.rst).

## Configuration

Profiles live in `~/.config/xnatctl/config.yaml` and store connection details
per server:

```yaml
default_profile: production

profiles:
  production:
    url: https://xnat.example.org
    username: myuser          # optional -- can also use env vars
    password_source: keyring  # password lives in the OS keychain
    default_project: MYPROJECT

  development:
    url: https://xnat-dev.example.org
    ca_bundle: /etc/ssl/dev-ca.pem   # trust a private CA instead of
                                     # switching verification off
```

```bash
xnatctl config add-profile dev --url https://xnat-dev.example.org  # add a server
xnatctl config use-context dev                                     # switch profiles
xnatctl config set-password production                             # store password in the OS keychain
```

`config set-password` (and `password_source: keyring`) needs the keyring
extra: `pip install "xnatctl[keyring]"`. It is not included in the plain
package or the standalone binary; an inline `password:` in the config file
works everywhere.

Credential priority, highest first: `XNAT_TOKEN` (an existing session token,
skips login entirely), then `--username` with `--password-stdin`, then
`XNAT_USER`/`XNAT_PASS` environment variables, then the profile config, then
an interactive prompt. Environment variables are the natural fit for CI jobs
and non-interactive scripts.

> **Passwords are never accepted as a command-line value.** `--password secret`
> is a usage error, because argv is visible in `ps` and your shell history.
> Use `--password-stdin`, `XNAT_PASS`, a stored profile credential, or the
> prompt.

## Features

- **Resource-centric commands** -- `xnatctl project list`,
  `xnatctl session download`, `xnatctl scan show`.
- **Profile-based configuration** -- one config file for all your servers;
  switch with `--profile` or `config use-context`.
- **Consistent output** -- `--output json|table` and `--quiet`, built for
  both humans and pipes.
- **Parallel operations** -- batch uploads and downloads fan out across
  multiple workers (`--workers`) with real-time progress.
- **Cached authentication** -- log in once and the session token is cached;
  commands re-authenticate automatically when stored credentials allow it.
- **Pure HTTP** -- talks directly to the XNAT REST API via httpx. Where
  [pyxnat](https://pyxnat.github.io/pyxnat/) and
  [xnatpy](https://xnat.readthedocs.io/) are Python libraries to import into
  your own code, xnatctl is a CLI-first tool for shell workflows -- and ships
  as a single binary with no Python environment required.

## Documentation

Complete documentation is in the [docs/](docs/) directory: installation, key
concepts, configuration, CLI reference, downloading, uploading, workflows, and
XNAT compatibility.

## Development

```bash
git clone https://github.com/rickyltwong/xnatctl.git
cd xnatctl
uv sync --dev

uv run pytest tests/ -v                  # tests
uv run ruff check xnatctl tests scripts  # lint
uv run mypy xnatctl                      # type check
```

## License

MIT
