Debugging
=========

When a command fails or hangs, xnatctl can show you what it is doing on the
wire. This page describes the three verbosity tiers and what healthy output
looks like, so you can tell an unusual line from a normal one.

Verbosity tiers
---------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Tier
     - What you get
   * - default
     - Warnings and errors only. Retries are reported, because a retry storm is
       the most common cause of an apparent hang.
   * - ``-v`` / ``--verbose``
     - xnatctl's own debug lines (one per HTTP attempt, and for every
       credential and session-cache decision), plus
       ``httpx`` at INFO -- one line per request. Also prints a traceback on
       unexpected errors.
   * - ``XNATCTL_DEBUG=1``
     - Everything above plus ``httpcore`` at DEBUG: a full connection-level
       trace. Useful for TLS and proxy problems, very noisy for anything else.

``XNATCTL_DEBUG`` is read before command-line flags are parsed, so use it when
the failure happens during startup -- a broken config file, an unresolvable
profile -- and ``-v`` never gets a chance to take effect. ``--quiet`` overrides
both: an explicit flag beats an ambient environment variable.

All logging goes to **stderr**, so it never contaminates ``-o json`` output
being piped into another tool.

A healthy authentication flow
-----------------------------

.. code-block:: console

   $ xnatctl -v auth login --profile prod
   [DEBUG] xnatctl.core.auth: Environment credentials: XNAT_USER=unset, XNAT_PASS=unset
   [DEBUG] xnatctl.core.auth: Session cache miss: /home/you/.config/xnatctl/.session does not exist
   [DEBUG] xnatctl.core.client: Authenticated as admin at https://xnat.example.org
   [DEBUG] xnatctl.core.auth: Cached session for admin at /home/you/.config/xnatctl/.session (expires 2026-07-28T14:15:00)

The credential lines report *where* each value came from and whether it was
set -- never the value itself. A password or token appearing in this output is
a bug; please report it.

On the next command the cache is warm:

.. code-block:: console

   $ xnatctl -v project list
   [DEBUG] xnatctl.core.auth: Session cache hit for https://xnat.example.org (user admin)
   [DEBUG] xnatctl.core.client: GET https://xnat.example.org/data/projects?columns=ID%2Cname%2Cpi_lastname%2Cdescription&format=json -> 200 in 143ms (attempt 1/4)

A ``cache miss`` line naming a *different* URL than the one you are targeting
means the cached session belongs to another server and is being ignored --
expected when you switch profiles.

A retried request
-----------------

Retries are logged at WARNING, so they appear without ``-v``:

.. code-block:: console

   $ xnatctl project list
   [WARNING] xnatctl.core.client: HTTP 503 on GET /data/projects; retrying in 1.4s (attempt 1/4)
   [WARNING] xnatctl.core.client: HTTP 503 on GET /data/projects; retrying in 3.1s (attempt 2/4)

The backoff is jittered, so the delays differ between runs even for the same
failure -- that is deliberate, to stop many clients retrying in lockstep. When
the server sends a ``Retry-After`` header, that value is used verbatim and the
line says ``per Retry-After``.

If you see the attempt counter climb to its limit, the command ends in
``RetryExhaustedError``; the server is genuinely unhealthy rather than xnatctl
being stuck.

Requests that are deliberately *not* retried
--------------------------------------------

A ``POST``/``PATCH`` that times out after the request was sent is not retried
automatically, and the error says so. The server may have executed it: retrying
could archive a session twice or launch a pipeline twice. Check server state
before repeating the command by hand.

Secrets in log output
---------------------

Every log record passes through a redaction filter that rewrites secret-shaped
URL query values (``token``, ``password``, ``api_key``, and similar) to ``***``,
and strips the password out of any ``user:pass@host`` URL. Non-secret query
parameters are left readable so the URL stays diagnosable.

One boundary worth knowing: the filter covers log *messages*. Exception
tracebacks are rendered after filters run, so if you are pasting a traceback
into a bug report, skim it first.

The diagnostics log file
-------------------------

The tiers above only exist on **stderr**, for the duration of one terminal
session. ``--log-file PATH`` writes a second, persistent copy of the full
xnatctl debug stream to a file, as JSON lines -- independent of whatever
verbosity stderr is showing. This is the thing to reach for after a long
run fails and the terminal has already scrolled past the useful part: hand
the file to whoever is debugging it, without re-running the command.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --log-file ~/xnatctl-diag.log

Three ways to turn it on, in precedence order (first one set wins):

1. ``--log-file PATH``, given at the root or on the subcommand itself
2. the ``XNATCTL_LOG_FILE`` environment variable
3. a ``log_file: PATH`` key at the top of ``config.yaml``

The flag and the environment variable work on every command, including the
handful that skip the usual global-option plumbing entirely (``config
init``/``use-context``/``current-context``/``add-profile``/``remove-
profile``/``set-password``, every ``dicom`` subcommand, every ``completion``
subcommand, ``project transfer-init``, ``local extract``). The config-file
tier is the one exception: those same commands do not read ``config.yaml``
for this feature, so ``log_file:`` alone will not activate diagnostics for
them -- use ``--log-file``/``XNATCTL_LOG_FILE`` there instead.

There is no default path -- one of the three above must name one explicitly.
The file is JSON Lines (one object per line: ``ts``, ``level``, ``logger``,
``corr``, ``msg``, and an ``exc`` field when the record carries an exception),
opened 0600, and rotates once at 10 MB to ``PATH.1``. Every run appends,
never truncates; a ``corr`` field (a short id, constant for one invocation,
different on the next) is what separates one run's lines from another's in a
long-lived file. The redaction filter covers this file the same way it covers
stderr, and closes the one gap noted above: a traceback logged to the file is
redacted too.

Because the point of this file is to survive a failure, it captures xnatctl's
full ``DEBUG`` stream regardless of ``--quiet``/``--verbose`` -- a plain
``xnatctl session download`` with ``--log-file`` set produces a quiet terminal
and a complete diagnostic file. The httpcore wire trace is the one thing kept
out of it by default; add ``XNATCTL_DEBUG=1`` alongside ``--log-file`` if you
need that level of detail captured too.

Whatever ends the command -- a caught error, ``Ctrl+C``, a declined
confirmation, or an early exit like ``whoami``'s "not authenticated" -- gets
one record in the file even when nothing else in the command logged
anything, tagged ``"event"`` (``command_failed``/``cancelled``/``exit``) so
you can jump straight to it. It never duplicates the traceback stderr already
shows under ``-v``; it is the *only* copy when stderr is quiet.

The file is scoped to xnatctl's own log stream (plus httpx/httpcore under the
wire-trace tier above) -- a dependency's own internal debug logging, if it has
any, is deliberately excluded, since it is not redacted the way xnatctl's own
log calls are.

The handler behind this stays attached to the process for as long as the
process runs. That is irrelevant for the CLI itself -- one process per
invocation -- but matters if you are calling xnatctl's Python API repeatedly
in one long-lived process: a second operation without ``--log-file`` (or with
a different one) keeps appending to the first file unless you close it
yourself first, via ``xnatctl.core.logging.remove_log_file()``.

This JSON shape is documented as **Unstable** (see :doc:`stability`): it is a
diagnostic artifact for a human or an AI assistant to read, not a scripted
interface, so fields may be added or renamed between releases.
