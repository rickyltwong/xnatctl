Contributing
============

Thank you for your interest in contributing to xnatctl. Whether you are filing a
bug report, suggesting a feature, or submitting code, your help is welcome.

xnatctl follows a layered architecture -- Click CLI commands delegate to a service
layer, which in turn calls the httpx-based HTTP client. Pydantic models define the
XNAT resource schemas, and Rich handles terminal output. If you are new to the
codebase, the `Architecture Overview`_ section below is a good place to start.


Reporting Bugs
--------------

If you encounter a bug, please open an issue on
`GitHub Issues <https://github.com/rickyltwong/xnatctl/issues>`_ with as much
detail as possible. A good bug report includes:

- **xnatctl version** -- run ``xnatctl --version`` and paste the output.
- **XNAT server version** -- run ``xnatctl health ping`` or ``xnatctl api get /xapi/siteConfig/buildInfo/version``.
- **Exact command** -- the full command you ran, e.g. ``xnatctl session list -P MY_PROJECT``.
- **Full error output** -- copy the complete traceback or error message.
- **OS and Python version** -- e.g. "Ubuntu 22.04, Python 3.12.1".

.. warning::

   Before pasting any output, make sure to remove or redact credentials, tokens,
   and any Protected Health Information (PHI). Never include passwords or session
   tokens in issue reports.

.. tip::

   Running with ``--verbose`` often surfaces additional detail that helps with
   diagnosis.


Requesting Features
-------------------

Feature requests are welcome. Open a
`GitHub issue <https://github.com/rickyltwong/xnatctl/issues>`_ and describe the
use case you are trying to solve. Explaining *why* you need the feature -- not just
*what* -- helps maintainers evaluate and prioritize the request.


Development Setup
-----------------

The development environment uses `uv <https://docs.astral.sh/uv/>`_ for fast
dependency resolution and virtual environment management. After cloning the
repository, a single ``uv sync`` installs the package in editable mode along
with all development dependencies (pytest, ruff, mypy, pre-commit, and others).

.. code-block:: console

   $ git clone https://github.com/rickyltwong/xnatctl.git
   $ cd xnatctl
   $ uv sync --dev

You can verify that the installation works by running the CLI:

.. code-block:: console

   $ uv run xnatctl --help


Running Tests
-------------

The test suite uses `pytest <https://docs.pytest.org/>`_. To run the full suite
with verbose output:

.. code-block:: console

   $ uv run pytest tests/ -v

With coverage reporting:

.. code-block:: console

   $ uv run pytest tests/ --cov=xnatctl --cov-report=html

The tests are organized by layer:

- ``tests/test_cli_*.py`` -- CLI integration tests that invoke Click commands via
  ``CliRunner``. These test argument parsing, output formatting, and error handling
  without making real HTTP requests.
- ``tests/test_service_*.py`` -- Service layer unit tests that mock ``XNATClient``
  to verify business logic, pagination, and error mapping in isolation.
- Other files cover the core client, configuration, authentication, validation, and
  upload/download helpers.

To run a single test file:

.. code-block:: console

   $ uv run pytest tests/test_cli_project.py -v

To run a single test function:

.. code-block:: console

   $ uv run pytest tests/test_cli_project.py::test_project_list_table -v

.. tip::

   Use ``-k`` to run tests matching a keyword expression, e.g.
   ``uv run pytest tests/ -k "upload and not dicom" -v``.


Linting, Formatting, and Type Checking
---------------------------------------

All three checks must pass before code is merged.

**Linting** catches style violations, unused imports, and common bugs:

.. code-block:: console

   $ uv run ruff check xnatctl

**Formatting** enforces consistent code layout (line length 100, PEP 8 conventions):

.. code-block:: console

   $ uv run ruff format xnatctl

**Type checking** verifies that type annotations are consistent and catches type
errors at development time:

.. code-block:: console

   $ uv run mypy xnatctl

.. note::

   Ruff combines the roles of flake8, isort, and Black in a single tool. You do
   not need to install those separately.


Pre-commit Hooks
----------------

The project uses `pre-commit <https://pre-commit.com/>`_ hooks to catch issues
before they reach CI. Install the hooks once after cloning:

.. code-block:: console

   $ uv run pre-commit install
   $ uv run pre-commit install --hook-type pre-push

Hooks that run on every **commit**:

- ``ruff check`` -- catches lint violations (unused imports, style issues, potential
  bugs).
- ``ruff format --check`` -- ensures code is formatted consistently.
- ``mypy`` -- verifies type annotations are correct.

Hooks that run on **push**:

- ``pytest`` -- runs the full test suite to prevent broken code from reaching the
  remote.


Building Documentation
----------------------

The documentation is built with `Sphinx <https://www.sphinx-doc.org/>`_. Install
the docs dependencies and build HTML output:

.. code-block:: console

   $ uv sync --dev --extra docs
   $ cd docs
   $ make html

The built docs are in ``docs/_build/html/``. Open ``index.html`` in a browser to
preview.


Architecture Overview
---------------------

xnatctl follows a layered design that separates concerns into three tiers:

**CLI layer** (``xnatctl/cli/``). Each resource type -- projects, subjects,
sessions, scans, resources, prearchive, pipelines -- has its own Click command
group. Commands parse arguments, set up context, call into the service layer, and
format output. They do not contain business logic or construct HTTP requests
directly.

**Service layer** (``xnatctl/services/``). Services encapsulate the XNAT REST API.
Each service extends ``BaseService``, which provides ``_get``, ``_post``,
``_paginate``, and ``_extract_results`` helpers. Services translate between Pydantic
models and raw API responses. For example, ``ProjectService.list()`` calls
``_get("/data/projects")``, extracts the result set, and returns a list of
``Project`` model instances.

**Core layer** (``xnatctl/core/``). The ``XNATClient`` wraps httpx with retry
logic (exponential backoff on 502/503/504), automatic re-authentication on 401,
pagination support, and session token management. The config module handles
YAML-based profiles and environment variable overrides. The output module uses Rich
to render tables, JSON, and quiet (ID-only) formats.

The CLI decorator stack composes behavior declaratively. A typical command looks
like this:

.. code-block:: python

   @project.command("list")
   @global_options       # --profile, --output, --quiet, --verbose
   @require_auth         # ensures authenticated client; re-auths on expiry
   @handle_errors        # catches XNATCtlError -> formatted error + sys.exit(1)
   def project_list(ctx: Context) -> None:
       service = ProjectService(ctx.client)
       projects = service.list()
       ctx.output(projects)

Destructive commands add ``@confirm_destructive`` (for ``--yes`` / ``--dry-run``
flags), and batch commands add ``@parallel_options`` (for ``--parallel`` /
``--workers``).

**Pydantic models** (``xnatctl/models/``) define the schema for each XNAT resource
type. Models use ``populate_by_name=True`` to accept XNAT API field aliases (e.g.,
``subject_ID``), ``extra="ignore"`` to tolerate unknown fields, and expose
``table_columns()`` and ``to_row()`` methods for Rich table rendering.

You can also use the service layer programmatically outside the CLI:

.. code-block:: python

   from xnatctl.core.client import XNATClient
   from xnatctl.services.projects import ProjectService

   client = XNATClient(base_url="https://xnat.example.org", ...)
   client.authenticate()

   service = ProjectService(client)
   projects = service.list()


Code Style
----------

xnatctl targets **Python 3.11+** and uses type hints throughout.

- **Formatting and linting**: `Ruff <https://docs.astral.sh/ruff/>`_ handles both,
  configured with a line length of 100 and rule sets E, F, W, I, B, and UP.
- **Docstrings**: Google-style with ``Args``, ``Returns``, and ``Raises`` sections.
  Every public function, class, and method must have a docstring.
- **Type checking**: `mypy <https://mypy-lang.org/>`_ with ``check_untyped_defs``
  enabled. Avoid ``Any`` unless absolutely necessary.
- **CLI framework**: `Click <https://click.palletsprojects.com/>`_ for command
  definitions, argument parsing, and help text.
- **Data models**: `Pydantic v2 <https://docs.pydantic.dev/>`_ for XNAT resource
  schemas with strict validation.
- **HTTP client**: `httpx <https://www.python-httpx.org/>`_ for synchronous HTTP
  with connection pooling and timeout control.
- **Terminal output**: `Rich <https://rich.readthedocs.io/>`_ for tables, progress
  bars, and styled error messages.

.. note::

   The project uses ``ruff`` in place of Black, isort, and flake8. Configuration
   lives in ``pyproject.toml`` under ``[tool.ruff]``.
