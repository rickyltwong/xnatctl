Contributing
============

Development Setup
-----------------

.. code-block:: console

   $ git clone https://github.com/rickyltwong/xnatctl.git
   $ cd xnatctl
   $ uv sync --dev

Running Tests
-------------

.. code-block:: console

   $ uv run pytest tests/ -v

With coverage:

.. code-block:: console

   $ uv run pytest tests/ --cov=xnatctl --cov-report=html

Linting and Formatting
----------------------

.. code-block:: console

   $ uv run ruff check xnatctl
   $ uv run ruff format xnatctl

Type Checking
-------------

.. code-block:: console

   $ uv run mypy xnatctl --ignore-missing-imports

Pre-commit Hooks
----------------

The project uses pre-commit hooks to catch issues before they reach CI:

.. code-block:: console

   $ uv run pre-commit install
   $ uv run pre-commit install --hook-type pre-push

Hooks that run on every commit:

- ``ruff check`` -- linting
- ``ruff format --check`` -- formatting
- ``mypy`` -- type checking

Hooks that run on push:

- ``pytest`` -- full test suite

Building Documentation
----------------------

.. code-block:: console

   $ uv sync --dev --extra docs
   $ cd docs
   $ make html

The built docs are in ``docs/_build/html/``.

Code Style
----------

- Python 3.11+; type hints throughout
- `Ruff <https://docs.astral.sh/ruff/>`_ for linting and formatting (line length 100)
- Google-style docstrings with ``Args``, ``Returns``, ``Raises``
- ``mypy`` for static type checking
- ``pytest`` for testing
- `Click <https://click.palletsprojects.com/>`_ for CLI framework
- `Pydantic <https://docs.pydantic.dev/>`_ for data models
- `httpx <https://www.python-httpx.org/>`_ for HTTP client
- `Rich <https://rich.readthedocs.io/>`_ for terminal output

Architecture
------------

The codebase follows a service layer pattern:

- **CLI layer** (``xnatctl/cli/``): Click commands that parse arguments and call services
- **Service layer** (``xnatctl/services/``): Business logic and XNAT REST API operations
- **Core layer** (``xnatctl/core/``): HTTP client, configuration, authentication, output formatting
- **Models** (``xnatctl/models/``): Pydantic data models for XNAT resources

.. code-block:: python

   from xnatctl.core.client import XNATClient
   from xnatctl.services.projects import ProjectService

   client = XNATClient(base_url="https://xnat.example.org", ...)
   client.authenticate()

   service = ProjectService(client)
   projects = service.list()
