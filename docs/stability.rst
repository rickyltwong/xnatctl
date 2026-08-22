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
   * - Top-level ``xnatctl`` exports (the names listed in ``xnatctl.__all__``)
     - Stable
     - Exactly the names in ``xnatctl.__all__`` -- the client, config, the
       service classes it lists, resource/progress models, the exception
       hierarchy, and ``xnatctl.__version__`` (see :doc:`api/core`,
       :doc:`api/services`, :doc:`api/models`). This is a membership test on
       ``__all__``, not a naming convention: ``__version__`` is
       underscore-prefixed and Stable because it is listed there, while a
       name with no leading underscore is still Provisional if it is not.
       Breaking changes go in a MINOR release while the project stays on
       ``0.x``, and are listed under ``**Breaking**`` in the
       :doc:`changelog`. See :doc:`adr/0014-promote-library-surface-to-stable`.
   * - Anything reachable on the ``xnatctl`` module or a submodule that is
       *not* listed in ``xnatctl.__all__``
     - Provisional
     - Covers three cases: a class that exists but isn't top-level-exported
       (``UserService``, ``XsyncService`` -- reachable only via
       ``xnatctl.services.users``/``xnatctl.services.xsync``, with no
       ``client.users``/``client.xsync`` accessor either);
       ``xnatctl.core.*``/``xnatctl.services.*`` internals reached by
       importing the submodule directly instead of the top-level name; and
       any ``_``-prefixed name. All three are documented and intentional, but
       move between minor releases -- pin an exact version if you import them.
       ``BaseService._get``/``_post`` remain the sanctioned extension point
       for a subclass, but "sanctioned" is not "semver-covered."


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

The 0.x label reflects that commands and top-level library exports are still
being *added*, not that scripted or library use is unsafe.


If something breaks anyway
--------------------------

Report it at https://github.com/rickyltwong/xnatctl/issues. A Stable surface
changing without notice is a bug, not a judgement call.
