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


Architecture Decisions
----------------------

A change that constrains future code -- a retry policy, a URL convention, a
lint rule deliberately left off -- needs its reasoning recorded. Decision
records are kept by the maintainer outside this repository; in a pull request,
state the decision and its rationale in the PR description and leave a short
comment at the constraint itself.

The test is whether the decision would surprise someone reading only the code.
If it would, the reasoning belongs somewhere it will be found before it is
"cleaned up".

Non-Goals
---------

**Internationalization.** xnatctl is English-only by design and carries no
gettext or locale infrastructure. Its audience is technical operators, and
the choice keeps message text independent of the runtime's locale. Wording
may still change between releases -- scripts should parse ``--output json``,
``--quiet`` output, and exit codes, the surfaces the
:doc:`stability policy <stability>` actually covers, never message text.
Do not introduce
translation machinery, locale-dependent formatting, or language-switching
options in a contribution; a change to this stance is a design decision to
raise with the maintainer first, not a patch.

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
  to verify business logic and error mapping in isolation.
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

.. note::

   CI enforces a coverage floor (see ``fail_under`` in ``pyproject.toml``), so
   substantial new code needs tests to keep the gate green.


The Integration Tier
--------------------

Everything above runs against mocks. That proves xnatctl sends what we think
it sends; it cannot prove XNAT answers the way we think it answers. The tests
in ``tests/integration/`` talk to a real server, and they are where beliefs
about XNAT's behaviour get checked -- whether an import lands in the archive
or the prearchive, what a scan ZIP's internal layout really is, whether the
files come back byte-identical.

These tests are marked ``integration`` and **deselected by default**, so a
normal ``pytest`` run never waits on any of it.

Start a throwaway XNAT and run them:

.. code-block:: console

   $ docker compose -f docker-compose.integration.yml up -d --wait
   $ uv run pytest tests/integration -m integration -v
   $ docker compose -f docker-compose.integration.yml down -v

The first ``up`` builds the image, which downloads the official XNAT WAR
(around 250 MB). Later runs reuse it. Startup takes roughly 90 seconds once
built, and ``--wait`` blocks until the server answers.

The stack listens on ``127.0.0.1:8104`` -- not 8080, which is the port most
likely to already be taken -- with credentials ``admin``/``admin``. The
fixtures complete XNAT's first-run setup wizard automatically; without that,
even ``POST /data/JSESSION`` is redirected to ``/setup``.

To point the tier at a server of your own instead:

.. code-block:: console

   $ XNATCTL_TEST_URL=https://xnat.example.org \
       XNATCTL_TEST_USER=me XNATCTL_TEST_PASS=secret \
       XNATCTL_TEST_I_KNOW_THIS_IS_NOT_PRODUCTION=yes \
       uv run pytest tests/integration -m integration -v

**Use a scratch server.** The tier creates projects and deletes them with
``removeFiles=true``. Because a stale ``XNATCTL_TEST_URL`` in a shell or a CI
variable is all it would take to run that against real imaging data, any host
other than localhost is refused unless
``XNATCTL_TEST_I_KNOW_THIS_IS_NOT_PRODUCTION=yes`` is also set.

If nothing is listening, the whole tier skips with a message saying so rather
than failing. In CI it runs nightly and on demand through the ``Integration``
workflow, not on pull requests -- see ``.github/workflows/integration.yml``.

To change the XNAT version under test, set ``XNAT_VERSION`` (it defaults to
the version the maintainers run in production) and rebuild:

.. code-block:: console

   $ XNAT_VERSION=1.9.3.6 docker compose -f docker-compose.integration.yml build


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
and ``_extract_results`` helpers. Services translate between Pydantic
models and raw API responses. For example, ``ProjectService.list()`` calls
``_get("/data/projects")``, extracts the result set, and returns a list of
``Project`` model instances.

**Core layer** (``xnatctl/core/``). The ``XNATClient`` wraps httpx with retry
logic (jittered exponential backoff on 429/500/502/503/504, honoring
``Retry-After``; transport failures such as a dropped connection are retried
only for idempotent methods, since a retried POST could execute twice),
automatic re-authentication on 401, and session token management. The
config module handles YAML-based profiles and environment
variable overrides. The output module uses Rich to render tables, JSON, and
quiet (ID-only) formats; log output passes through a redaction filter that
scrubs secret-shaped URL values.

The CLI decorator stack composes behavior declaratively. A typical command looks
like this:

.. code-block:: python

   @project.command("list")
   @global_options       # --profile, --output, --quiet, --verbose, --no-color
   @handle_errors        # catches XNATCtlError -> formatted error + sys.exit(1)
   @require_auth         # ensures authenticated client; re-auths on expiry
   def project_list(ctx: Context) -> None:
       service = ProjectService(ctx.client)
       projects = service.list()
       ctx.output(projects)

Destructive commands add ``@confirm_destructive`` (for ``--yes`` / ``--dry-run``
flags), and batch commands add ``@parallel_options`` (for ``--workers``).

**Pydantic models** (``xnatctl/models/``) define the schema for each XNAT resource
type. Models use ``populate_by_name=True`` to accept XNAT API field aliases (e.g.,
``subject_ID``), ``extra="ignore"`` to tolerate unknown fields, and expose
``table_columns()`` and ``to_row()`` methods for Rich table rendering.

You can also use the service layer programmatically outside the CLI:

.. code-block:: python

   import xnatctl

   with xnatctl.XNATClient.from_profile("prod") as client:
       projects = client.projects.list()

   # Or, hand-wired (what from_profile does for you): construct an
   # XNATClient with explicit credentials, call ``authenticate()``, and
   # pass it to any service class.

For the full step-by-step recipe -- model, service, CLI decorators, tests,
and docs -- see :doc:`adding-a-command`.


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
