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
need_cmd tar
need_cmd sha256sum

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

ASSET="xnatctl-linux-amd64.tar.gz"
BASE_URL="https://github.com/${REPO}/releases/download/${VERSION}"

TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

info "Downloading ${ASSET}..."
curl -fsSL -o "${TMPDIR}/${ASSET}" "${BASE_URL}/${ASSET}"

# Verify checksum if available
CHECKSUM_URL="${BASE_URL}/${ASSET}.sha256"
if curl -fsSL -o "${TMPDIR}/${ASSET}.sha256" "${CHECKSUM_URL}" 2>/dev/null; then
    info "Verifying checksum..."
    (cd "${TMPDIR}" && sha256sum -c "${ASSET}.sha256") || fail "Checksum verification failed"
else
    warn "Checksum file not available; skipping verification"
fi

info "Extracting to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
tar -xzf "${TMPDIR}/${ASSET}" -C "${INSTALL_DIR}"
chmod +x "${INSTALL_DIR}/xnatctl"

# Verify installation
if "${INSTALL_DIR}/xnatctl" --version >/dev/null 2>&1; then
    info "Installed $("${INSTALL_DIR}/xnatctl" --version 2>&1 || echo "xnatctl")"
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
