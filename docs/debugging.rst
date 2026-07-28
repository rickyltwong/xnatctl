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
     - xnatctl's own debug lines (one per HTTP attempt, per page of a paginated
       listing, and for every credential and session-cache decision), plus
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
   [DEBUG] xnatctl.core.client: GET https://xnat.example.org/data/projects?format=json&offset=0&limit=100 -> 200 in 143ms (attempt 1/4)
   [DEBUG] xnatctl.core.client: paginate /data/projects: offset=0 limit=100 -> 37 items

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
