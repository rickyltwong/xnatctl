# Contributing

Full guide: https://xnatctl.readthedocs.io/en/latest/contributing.html

Quickstart:

```bash
uv sync --dev
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
uv run pytest tests/ -v
uv run ruff check xnatctl tests scripts
```
