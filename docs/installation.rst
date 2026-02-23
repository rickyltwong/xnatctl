Installation
============

You can install xnatctl in three ways: as a **standalone binary** (no Python
required), as a **Python package** from PyPI, or via **Docker**. If you just
want to use the CLI and do not need Python library access, the standalone binary
is the fastest path.

Prerequisites
-------------

All you need is a terminal application (Terminal on macOS, PowerShell or
Windows Terminal on Windows, any shell on Linux). If you choose the Python
package method, you also need **Python 3.11 or later** installed on your
system.

Standalone Binary (recommended for most users)
-----------------------------------------------

The standalone binary is the simplest way to get started. It is a single
self-contained executable with no runtime dependencies -- you do not need
Python, pip, or any other tooling installed.

Pre-built binaries are published for the following platforms:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - Operating System
     - Architecture
     - Asset Name
   * - Linux
     - x86_64
     - ``xnatctl-linux-amd64.tar.gz``
   * - macOS
     - x86_64
     - ``xnatctl-darwin-amd64.tar.gz``
   * - Windows
     - x86_64
     - ``xnatctl-windows-amd64.zip``

One-line install script (Linux and macOS)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The install script auto-detects your operating system and architecture,
downloads the latest release from GitHub, verifies its checksum, and places the
binary in ``~/.local/bin``:

.. code-block:: console

   $ curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

To pin a specific version, set the ``XNATCTL_VERSION`` environment variable
before running the script:

.. code-block:: console

   $ XNATCTL_VERSION=v0.1.1 curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

To install into a custom directory instead of the default ``~/.local/bin``, set
``XNATCTL_INSTALL_DIR``:

.. code-block:: console

   $ XNATCTL_INSTALL_DIR=/usr/local/bin curl -fsSL https://github.com/rickyltwong/xnatctl/raw/main/install.sh | bash

.. tip::

   The install script automatically verifies the SHA-256 checksum of the
   downloaded binary when a ``.sha256`` file is available in the release. You
   do not need to verify the checksum manually.

.. note::

   The install script requires a Unix-like shell (bash) and is not available
   natively on Windows. Windows users should follow the manual download steps
   below or install via the Python package.

Manual download
^^^^^^^^^^^^^^^

If you prefer not to pipe a script into your shell, you can download the
appropriate archive from `GitHub Releases
<https://github.com/rickyltwong/xnatctl/releases>`_ and extract it yourself.

**Linux / macOS:**

.. code-block:: console

   $ tar -xzf xnatctl-linux-amd64.tar.gz
   $ chmod +x xnatctl
   $ mv xnatctl ~/.local/bin/

**Windows (PowerShell):**

Download ``xnatctl-windows-amd64.zip`` from the
`releases page <https://github.com/rickyltwong/xnatctl/releases>`_, then
extract and move the binary to a directory on your PATH:

.. code-block:: powershell

   Expand-Archive xnatctl-windows-amd64.zip -DestinationPath .
   # Create a directory for CLI tools if it does not exist
   New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\bin"
   Move-Item xnatctl.exe "$env:LOCALAPPDATA\bin\"

After extracting, verify that xnatctl runs:

.. code-block:: powershell

   xnatctl.exe --version

If you see ``xnatctl is not recognized`` or ``command not found``, you need to
add the install directory to your PATH. See the
:ref:`troubleshooting <installation-troubleshooting>` section below.

.. note::

   On Linux and macOS the default install location is ``~/.local/bin``. On
   Windows the examples above use ``%LOCALAPPDATA%\bin``
   (``C:\Users\<you>\AppData\Local\bin``). You can choose any directory that is
   on your PATH.

Python Package
--------------

Choose the Python package if you already have a Python 3.11+ environment, want
to import xnatctl as a library in your own scripts, or need the optional DICOM
utilities.

**Install from PyPI (recommended):**

.. code-block:: console

   $ pip install xnatctl

**Install with uv** (faster alternative to pip):

.. code-block:: console

   $ uv pip install xnatctl

**With DICOM extras:**

The ``dicom`` extra installs `pydicom <https://pydicom.github.io/>`_, which
enables the ``xnatctl dicom validate`` and ``xnatctl dicom inspect`` commands
for local DICOM file inspection before upload:

.. code-block:: console

   $ pip install "xnatctl[dicom]"

**Install from source:**

If you want to track the development branch or contribute to xnatctl, you can
install directly from the Git repository:

.. code-block:: console

   $ git clone https://github.com/rickyltwong/xnatctl.git
   $ cd xnatctl
   $ uv sync

.. tip::

   It is good practice to install Python CLI tools inside a virtual environment
   to avoid conflicts with system packages. You can create one with
   ``python -m venv .venv`` and activate it before running ``pip install``, or
   use ``uv`` which manages environments automatically.

Docker
------

If you are running xnatctl in a CI pipeline or prefer a fully isolated
environment, you can use the Docker image. No local installation is required
beyond Docker itself:

.. code-block:: console

   $ docker run --rm ghcr.io/rickyltwong/xnatctl:main --help

Verifying Your Installation
----------------------------

After installing xnatctl by any method, confirm that the binary is available
and prints its version:

.. code-block:: console

   $ xnatctl --version

You should see output similar to ``xnatctl, version 0.1.1``.

You can also run the built-in help to see the full list of available commands:

.. code-block:: console

   $ xnatctl --help

This prints the top-level command groups (``project``, ``session``, ``auth``,
etc.) along with global options like ``--output``, ``--profile``, and
``--quiet``.

.. _installation-troubleshooting:

Troubleshooting
---------------

Below are solutions to common installation issues.

**"command not found" after installing**

Your shell cannot find the ``xnatctl`` binary because its install directory is
not on your ``PATH``.

*Linux (bash):*

.. code-block:: console

   $ echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
   $ source ~/.bashrc

*macOS (zsh):*

.. code-block:: console

   $ echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   $ source ~/.zshrc

*Windows (PowerShell):*

Add ``%LOCALAPPDATA%\bin`` to your user PATH permanently. Run this in an
**elevated** (Administrator) PowerShell, or use Settings > System >
About > Advanced system settings > Environment Variables:

.. code-block:: powershell

   $binDir = "$env:LOCALAPPDATA\bin"
   $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
   if ($currentPath -notlike "*$binDir*") {
       [Environment]::SetEnvironmentVariable("Path", "$currentPath;$binDir", "User")
   }

After updating the PATH, close and reopen your terminal for the change to take
effect.

**"permission denied" when running the binary**

The binary needs the executable permission bit set. This is handled
automatically by the install script, but if you downloaded manually you may
need to set it yourself:

.. code-block:: console

   $ chmod +x ~/.local/bin/xnatctl

**SSL certificate errors**

On some older systems the default CA certificate bundle may be outdated,
causing SSL verification failures when connecting to your XNAT server. You can
work around this by updating your system certificates or, as a temporary
measure, disabling SSL verification in your xnatctl profile:

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org --no-verify-ssl

.. warning::

   Disabling SSL verification removes protection against man-in-the-middle
   attacks. Only use ``--no-verify-ssl`` on trusted networks and update your
   system certificates as soon as possible.

**Python version too old**

xnatctl requires Python 3.11 or later. If ``pip install xnatctl`` fails with a
resolver or syntax error, check your Python version:

.. code-block:: console

   $ python3 --version

If the version shown is older than 3.11, upgrade Python through your system
package manager or download it from `python.org <https://www.python.org/downloads/>`_.
