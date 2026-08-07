Stability and Deprecation Policy
=================================

This page says what you can pin a script to, and how much notice you get
before something you depend on changes.

It matters because xnatctl is meant to be scripted. A flag that vanishes
between patch releases breaks a cron job at 3am, and a JSON field that
quietly changes shape breaks it silently, which is worse.


What is covered
---------------

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Surface
     - Tier
     - What that means
   * - Command and subcommand names
     - Stable
     - Removed or renamed only in a MAJOR release.
   * - Documented flags (visible in ``--help``)
     - Stable
     - Removed only after a deprecation period; see below.
   * - ``--output json`` field names and types
     - Stable
     - Existing fields keep their name, type, and meaning. New fields may be
       added at any time, so parse defensively -- do not assume a fixed key set.
   * - ``--quiet`` output
     - Stable
     - One identifier per line, nothing else. Safe for ``xargs``.
   * - Exit codes
     - Stable
     - ``0`` success, non-zero failure, and a code that already means something
       keeps meaning it. New codes may be added for cases that previously
       exited ``1``, so test for ``!= 0`` rather than ``== 1``.
   * - ``--output table`` layout
     - Unstable
     - Column set, order, and widths are presentation. Do not parse it; use
       ``--output json``.
   * - Log and progress text on stderr
     - Unstable
     - Wording changes freely.
   * - Hidden flags (absent from ``--help``)
     - Unstable
     - Mostly deprecated aliases kept alive for existing scripts, plus a few
       internal switches. Do not adopt them in new work.
   * - The Python package API (``import xnatctl``)
     - Provisional
     - The CLI is the product. Importable modules are documented in the
       :doc:`api/core` reference, but names and signatures move as internals
       change. Pin an exact version if you import them.


How deprecation works
---------------------

When a flag is replaced, the old one keeps working. Using it prints a warning
on **stderr** -- never stdout, so it cannot corrupt a piped ``--output json``
or ``--quiet`` result:

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --unzip --out ./data
   Warning: --unzip is deprecated and will be removed in 0.5.0; use --extract instead

The warning names the release that removes the flag, so you can tell from a
single log line how long you have.

The rules:

1. A deprecated flag survives at least **two MINOR releases** after the release
   that deprecated it.
2. The removal release is decided at deprecation time and named in the warning.
   It does not move earlier.
3. Removal happens in the named release, and only there.
4. Every deprecated flag is registered in ``DEPRECATED_FLAGS`` in
   ``xnatctl/cli/common.py``. A test walks the whole command tree and fails on
   any deprecated option missing from that table, so a flag cannot be retired
   without a dated warning.

Deprecated flags are hidden from ``--help`` on the day they are deprecated.
They still work; they are just no longer advertised.


Currently deprecated
--------------------

All of the flags below are removed in **0.5.0**.

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Deprecated
     - Use instead
     - Commands
   * - ``--unzip``
     - ``--extract``
     - ``session download``, ``scan download``
   * - ``--no-unzip``
     - ``--no-extract``
     - ``session download``, ``scan download``
   * - ``--no-cleanup``
     - ``--extract --keep-zips``
     - ``session download``, ``scan download``
   * - ``--cleanup``
     - nothing; cleanup is implicit with ``--extract``
     - ``session download``, ``scan download``
   * - ``--include-resources``
     - ``--session-resources``
     - ``session download``
   * - ``--no-parallel``
     - ``--workers 1``
     - ``admin refresh-catalogs``, ``project transfer``, ``scan delete``
   * - ``--parallel``
     - nothing; parallel is the default
     - ``admin refresh-catalogs``, ``project transfer``, ``scan delete``
   * - ``--session``
     - ``--experiment`` / ``-E``
     - ``session upload``, ``session upload-exam``
   * - ``--gradual``
     - ``--mode gradual``
     - ``session upload``
   * - ``--archive-format``
     - ``--mode``
     - ``session upload``


What 0.x means here
-------------------

xnatctl is below 1.0, and semantic versioning allows a 0.x project to break
anything at any time. This policy is the stricter promise made on top of that:
breaking changes to the surfaces marked Stable above go in MINOR releases, are
listed in the :doc:`changelog`, and -- for flags -- get the deprecation period
described above. Patch releases never break them.

The 0.x label reflects that commands are still being *added* and that the
Python API is still moving, not that scripted use is unsafe.


If something breaks anyway
--------------------------

Report it at https://github.com/rickyltwong/xnatctl/issues. A Stable surface
changing without notice is a bug, not a judgement call.
