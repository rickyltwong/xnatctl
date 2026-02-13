Installation
============

Requirements
------------

- Python 3.11 or later
- `uv <https://docs.astral.sh/uv/>`_ (recommended) or pip

Install from PyPI
-----------------

.. code-block:: console

   $ pip install xnatctl

Or with ``uv``:

.. code-block:: console

   $ uv pip install xnatctl

Install with DICOM support
---------------------------

.. code-block:: console

   $ pip install xnatctl[dicom]

Install from source
-------------------

.. code-block:: console

   $ git clone https://github.com/rickyltwong/xnatctl.git
   $ cd xnatctl
   $ uv sync

Verify the installation:

.. code-block:: console

   $ xnatctl --version
