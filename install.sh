#!/usr/bin/env bash
# install.sh - Install xnatctl standalone binary
#
# Usage:
#   curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash
#
# Environment variables:
#   XNATCTL_VERSION     - Version to install (default: latest release)
#   XNATCTL_INSTALL_DIR - Install directory (default: ~/.local/bin)

set -euo pipefail

REPO="rickyltwong/xnatctl"
INSTALL_DIR="${XNATCTL_INSTALL_DIR:-${HOME}/.local/bin}"

info() { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
fail() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

need_cmd curl

# Detect OS
detect_os() {
    local os
    os="$(uname -s)"
    case "${os}" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "darwin" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *) fail "Unsupported operating system: ${os}" ;;
    esac
}

# Detect architecture
detect_arch() {
    local arch
    arch="$(uname -m)"
    case "${arch}" in
        x86_64|amd64) echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) fail "Unsupported architecture: ${arch}" ;;
    esac
}

OS="$(detect_os)"
ARCH="$(detect_arch)"
info "Detected platform: ${OS}-${ARCH}"

if [ "${OS}" = "windows" ]; then
    need_cmd unzip
else
    need_cmd tar
fi

# Detect version
if [ -n "${XNATCTL_VERSION:-}" ]; then
    VERSION="${XNATCTL_VERSION}"
else
    info "Detecting latest release..."
    VERSION=$(
        curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
        | grep '"tag_name"' \
        | sed -E 's/.*"tag_name":\s*"([^"]+)".*/\1/'
    )
    [ -n "${VERSION}" ] || fail "Could not determine latest version"
fi
info "Installing xnatctl ${VERSION}"

if [ "${OS}" = "windows" ]; then
    ASSET="xnatctl-${OS}-${ARCH}.zip"
else
    ASSET="xnatctl-${OS}-${ARCH}.tar.gz"
fi
BASE_URL="https://github.com/${REPO}/releases/download/${VERSION}"

TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

info "Downloading ${ASSET}..."
curl -fsSL -o "${TMPDIR}/${ASSET}" "${BASE_URL}/${ASSET}"

# Verify checksum if available
CHECKSUM_URL="${BASE_URL}/${ASSET}.sha256"
if curl -fsSL -o "${TMPDIR}/${ASSET}.sha256" "${CHECKSUM_URL}" 2>/dev/null; then
    info "Verifying checksum..."
    if command -v sha256sum >/dev/null 2>&1; then
        (cd "${TMPDIR}" && sha256sum -c "${ASSET}.sha256") || fail "Checksum verification failed"
    elif command -v shasum >/dev/null 2>&1; then
        expected=$(awk '{print $1}' "${TMPDIR}/${ASSET}.sha256")
        actual=$(shasum -a 256 "${TMPDIR}/${ASSET}" | awk '{print $1}')
        [ "${expected}" = "${actual}" ] || fail "Checksum verification failed"
    else
        warn "No sha256sum or shasum found; skipping verification"
    fi
else
    warn "Checksum file not available; skipping verification"
fi

info "Extracting to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

if [ "${OS}" = "windows" ]; then
    unzip -o "${TMPDIR}/${ASSET}" -d "${INSTALL_DIR}"
else
    tar -xzf "${TMPDIR}/${ASSET}" -C "${INSTALL_DIR}"
    chmod +x "${INSTALL_DIR}/xnatctl"
fi

# Verify installation
BINARY="${INSTALL_DIR}/xnatctl"
if [ "${OS}" = "windows" ]; then
    BINARY="${INSTALL_DIR}/xnatctl.exe"
fi

if "${BINARY}" --version >/dev/null 2>&1; then
    info "Installed $("${BINARY}" --version 2>&1 || echo "xnatctl")"
else
    warn "Binary extracted but could not run --version"
fi

# Check PATH
case ":${PATH}:" in
    *":${INSTALL_DIR}:"*) ;;
    *)
        warn "${INSTALL_DIR} is not in your PATH"
        echo "  Add it with:  export PATH=\"${INSTALL_DIR}:\${PATH}\""
        ;;
esac
