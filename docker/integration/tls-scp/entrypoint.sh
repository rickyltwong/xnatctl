#!/bin/sh
# Generate a throwaway self-signed cert, then exec the SCP.
#
# Regenerated on every container start rather than baked into the image: see
# the Dockerfile for why. /certs is bind-mounted from the host so the test
# process (running outside any container) can read ca.pem directly, which is
# why the whole directory is opened up with a permissive chmod below --
# Docker creates a fresh bind-mount source directory as root, and the host
# user running pytest is essentially never uid 0.
set -eu

CERT_DIR="${TLS_CERT_DIR:-/certs}"
mkdir -p "$CERT_DIR"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.pem" \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    2>/dev/null

# Self-signed: the leaf certificate the server presents IS the trust anchor
# a client needs to load as its CA bundle.
cp "$CERT_DIR/server.pem" "$CERT_DIR/ca.pem"

# World-writable on purpose: this is a 1-day, no-real-secrecy-value test
# credential in a bind-mounted host directory that a non-root host user
# needs to be able to clean up.
chmod 0777 "$CERT_DIR"
chmod 0666 "$CERT_DIR"/*.pem "$CERT_DIR"/*.key

echo "tls-scp: certificate ready at $CERT_DIR (CN=localhost, 1 day validity)"

exec python3 /usr/local/bin/scp.py \
    --cert "$CERT_DIR/server.pem" \
    --key "$CERT_DIR/server.key" \
    --port "${TLS_SCP_PORT:-11112}" \
    --aet "${TLS_SCP_AET:-TLSSCP}"
