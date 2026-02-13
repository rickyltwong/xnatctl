Installation
============

Requirements
------------

- Python 3.11 or later
- `uv <https://docs.astral.sh/uv/>`_ (recommended) or pip

Standalone Binary (no Python required)
---------------------------------------

Download a self-contained Linux binary with no Python installation needed:

.. code-block:: console

   $ curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

Install a specific version or to a custom directory:

.. code-block:: console

   $ XNATCTL_VERSION=v0.1.0 curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash
   $ XNATCTL_INSTALL_DIR=/usr/local/bin curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

Or download manually from `GitHub Releases <https://github.com/rickyltwong/xnatctl/releases>`_:

.. code-block:: console

   $ tar -xzf xnatctl-linux-amd64.tar.gz
   $ chmod +x xnatctl
   $ mv xnatctl ~/.local/bin/
   $ xnatctl --version

Python Package
--------------

.. code-block:: console

   $ uv pip install git+https://github.com/rickyltwong/xnatctl.git

Or with pip:

.. code-block:: console

   $ pip install git+https://github.com/rickyltwong/xnatctl.git

With DICOM utilities (optional):

.. code-block:: console

   $ pip install "xnatctl[dicom] @ git+https://github.com/rickyltwong/xnatctl.git"

Docker
------

.. code-block:: console

   $ docker run --rm ghcr.io/rickyltwong/xnatctl:main --help

Install from Source
-------------------

.. code-block:: console

   $ git clone https://github.com/rickyltwong/xnatctl.git
   $ cd xnatctl
   $ uv sync

Verify the installation:

.. code-block:: console

   $ xnatctl --version
