# Both images are pinned by multi-arch index digest on FROM lines, which
# Dependabot's docker ecosystem keeps current.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3

WORKDIR /app

# Install exactly uv.lock's runtime set, hash-verified: the versions the test
# suite ran against. The build backend comes from the lock too (the `build`
# group): the project is built with that hatchling, with no build isolation
# and no network, so nothing unlocked can be fetched. The last sync removes
# the build group from the venv again.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --group build --no-install-project
COPY xnatctl ./xnatctl
RUN --network=none \
    uv sync --locked --offline --no-dev --group build --no-editable --no-build-isolation \
 && uv sync --locked --offline --no-dev --no-editable --no-build-isolation

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
