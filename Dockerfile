# Both images are pinned by multi-arch index digest on FROM lines, which
# Dependabot's docker ecosystem keeps current.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3

WORKDIR /app

# Install exactly uv.lock's runtime set, hash-verified: the versions the test
# suite ran against. A plain `pip install .` re-resolved them at build time.
# The build backend (hatchling) is still fetched by build isolation, unlocked.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY xnatctl ./xnatctl
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

LABEL org.opencontainers.image.source="https://github.com/rickyltwong/xnatctl"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

# pip is the base image's, never used at runtime (`xnatctl upgrade` in a
# container prints a `docker pull`), and it carries advisories that every
# image would otherwise ship. xnatctl runs as an unprivileged user; its
# config and session cache live under /home/xnatctl/.config/xnatctl.
RUN pip uninstall -y pip && useradd --create-home --uid 1000 xnatctl

COPY --from=builder /app/.venv /app/.venv

USER xnatctl
WORKDIR /home/xnatctl

ENTRYPOINT ["xnatctl"]
