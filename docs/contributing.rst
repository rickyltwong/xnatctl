Contributing
============

Development setup
-----------------

.. code-block:: console

   $ git clone https://github.com/rickyltwong/xnatctl.git
   $ cd xnatctl
   $ uv sync

Running tests
-------------

.. code-block:: console

   $ uv run pytest tests/ -v

Linting and formatting
----------------------

.. code-block:: console

   $ uv run ruff check xnatctl
   $ uv run ruff format xnatctl

Type checking
-------------

.. code-block:: console

   $ uv run mypy xnatctl

Code style
----------

- Python 3.11+; type hints throughout
- `Ruff <https://docs.astral.sh/ruff/>`_ for linting and formatting
- Google-style docstrings
- ``mypy`` for static type checking
- ``pytest`` for testing
