#!/usr/bin/env bash
# Audit the Python distributions installed inside a Docker image.
#
# Usage: scripts/audit_image.sh IMAGE
#
# The image is built with `pip install .`, which never reads uv.lock, so the
# lockfile audit does not see what the image actually ships (including
# whatever the base image brings, e.g. its own pip). This lists every
# installed distribution in IMAGE, drops xnatctl itself (it has no PyPI
# advisories of its own to match against a local build), and runs pip-audit
# on the rest. Exits nonzero on any finding, and also when the listing fails
# or comes back empty -- an empty list would otherwise audit as clean.
#
# Single source of truth: the scheduled audit workflow and the local release
# step both call this script.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 IMAGE" >&2
  exit 2
fi
image="$1"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# importlib.metadata rather than `pip freeze`: freeze hides pip itself, and
# the image may not have pip at all.
docker run --rm --entrypoint python "$image" -c '
import importlib.metadata as m

names = {(d.metadata["Name"], d.version) for d in m.distributions()}
for name, version in sorted(n for n in names if n[0]):
    print(f"{name}=={version}")
' > "$workdir/all.txt"

if [[ ! -s "$workdir/all.txt" ]]; then
  echo "error: no installed distributions found in $image" >&2
  exit 1
fi

grep -v -i '^xnatctl==' "$workdir/all.txt" > "$workdir/deps.txt" || true

if [[ ! -s "$workdir/deps.txt" ]]; then
  echo "error: no dependencies left to audit in $image" >&2
  exit 1
fi

echo "Auditing $(wc -l < "$workdir/deps.txt") distributions installed in $image"
# --no-deps: the list is already the complete installed set, so nothing is
# resolved and pip-audit needs neither pip nor a scratch environment.
uv tool run pip-audit -r "$workdir/deps.txt" --no-deps --disable-pip
