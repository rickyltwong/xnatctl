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
   * - ``--output tsv`` shape
     - Stable
     - The *shape* is stable, and depends on what the command returns:
       a list of records prints a header line of raw column keys (unless
       ``--no-headers``) followed by one tab-joined line per record; a
       single record prints the same header-plus-one-row shape; a bare
       scalar or a list of non-record values prints one sanitized value per
       line with **no** header line, ``--no-headers`` or not, because there
       are no field names to print; an empty list with no columns resolved
       prints nothing at all (not even a header) -- the missing line is the
       emptiness signal. Embedded tabs/newlines collapse to single spaces,
       and stray control bytes (including ANSI escapes) are stripped, so
       every record is exactly one line. The *column set* is not stable:
       without ``--columns``, list output falls back to the union of keys
       across all records in first-seen order, which is not guaranteed to
       match ``--output table``'s default columns. Scripts should pass
       ``--columns`` to pin an exact, ordered column set.
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
       :doc:`changelog`.
   * - Anything reachable on the ``xnatctl`` module or a submodule that is
       *not* listed in ``xnatctl.__all__``
     - Provisional
     - Covers three cases: a class that exists but isn't top-level-exported
       (``UserService``, ``XsyncService`` -- reachable only via
       ``xnatctl.services.users``/``xnatctl.services.xsync``, with no
       ``client.users``/``client.xsync`` accessor either; and
       ``AsyncXNATClient``, reachable only via ``xnatctl.core.async_client``
       -- it has landed in one release and has not yet proven its final
       shape, particularly whether the read-only/no-service-accessors
       boundary holds up under real use);
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

1. The removal release is at least **two MINOR releases** after the release
   that deprecated the flag (deprecated in 0.3.0 means removed no earlier
   than 0.5.0), whatever releases ship in between.
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

All of the flags below are removed in **0.7.0** -- deprecated to reconcile
argument conventions across the CLI: a stray ``-e`` short flag that should
have been ``-E`` all along, ``--file`` used for two different meanings
depending on the command, and a ``-s``/``-S`` collision on the same command
line.

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Deprecated
     - Use instead
     - Commands
   * - ``-e``
     - ``-E`` / ``--experiment``
     - ``pipeline run``, ``pipeline jobs``, ``admin refresh-catalogs``
   * - ``--file``
     - ``--output-file`` / ``-f``
     - ``resource download``
   * - ``-f``
     - ``--file`` (long form only)
     - ``api post``, ``api put``
   * - ``-f``
     - ``--follow`` (long form only)
     - ``container logs``
   * - ``-s``
     - ``--scans`` (long form only)
     - ``scan delete``, ``scan download``


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
