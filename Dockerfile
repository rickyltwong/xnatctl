FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md /app/
COPY xnatctl /app/xnatctl

# pip is build-time only: the runtime never calls it (`xnatctl upgrade` in a
# container prints a `docker pull`), and the base image's pip carries
# advisories that every image would otherwise ship.
RUN pip install --no-cache-dir . && pip uninstall -y pip

ENTRYPOINT ["xnatctl"]
