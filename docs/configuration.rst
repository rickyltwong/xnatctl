Configuration
=============

xnatctl organizes server connections through **profiles** -- named groups of settings
that describe how to reach a particular XNAT instance. Each profile stores a server URL,
SSL preferences, timeout, and an optional default project, so you never have to
re-type connection details between commands.

Profiles matter because most teams interact with more than one XNAT server. You might
have a production instance that hosts real study data, a development server for testing
pipelines, and perhaps a staging environment for validating upgrades. Profiles let you
switch between these servers with a single flag instead of juggling environment variables
or editing files by hand.

All configuration lives in a single YAML file at ``~/.config/xnatctl/config.yaml``.
xnatctl creates the ``~/.config/xnatctl/`` directory automatically the first time you
run ``config init``, and session tokens are cached in the same directory.


Creating Your First Configuration
----------------------------------

The easiest way to get started is with ``config init``, which walks you through the
minimum settings and writes the config file for you.

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org --project MYPROJECT

If you omit ``--url``, the command prompts you interactively. The ``--project`` flag is
optional but convenient -- it sets a default project so you can skip the ``-P`` flag on
most commands.

After running ``config init``, your configuration file will look like this:

.. code-block:: yaml

   # ~/.config/xnatctl/config.yaml
   default_profile: default          # The profile used when --profile is not specified
   output_format: table              # Global output format (table or json)

   profiles:
     default:
       url: https://xnat.example.org # XNAT server base URL (required)
       verify_ssl: true              # Verify TLS certificates
       timeout: 21600                # HTTP timeout in seconds (6 hours)
       default_project: MYPROJECT    # Fallback project for -E flag resolution

The ``default_profile`` key tells xnatctl which profile to use when you do not pass
``--profile`` on the command line. Since ``config init`` creates the first profile, it
is automatically marked as the default.


Working with Multiple Profiles
------------------------------

In practice, you will almost always have at least two XNAT environments: a production
server with real study data and a development server for testing new pipelines or
software upgrades. Profiles let you define both once and switch between them freely.

To add a second profile for your development server, use ``config add-profile``:

.. code-block:: console

   $ xnatctl config add-profile dev \
       --url https://xnat-dev.example.org \
       --no-verify-ssl \
       --project TESTPROJECT

You can then switch the active profile so that all subsequent commands target the
development server. The ``use-context`` and ``current-context`` command names follow
a convention from tools like ``kubectl`` -- they simply mean "switch to this profile"
and "show the active profile":

.. code-block:: console

   $ xnatctl config use-context dev
   $ xnatctl config show

To view which profile is currently active without the full configuration dump, use
``config current-context``:

.. code-block:: console

   $ xnatctl config current-context
   dev

.. tip::

   You do not have to change the active profile to run a one-off command against a
   different server. Pass ``--profile`` (available on every command) to override the
   default for that single invocation:

   .. code-block:: console

      $ xnatctl --profile production project list


Profile Fields Reference
------------------------

The table below describes every field you can set inside a profile. Only ``url`` is
required; the rest have sensible defaults.

.. list-table::
   :header-rows: 1
   :widths: 18 14 68

   * - Field
     - Default
     - Description
   * - ``url``
     - *(required)*
     - Base URL of the XNAT server, including the scheme (e.g.,
       ``https://xnat.example.org``). This is the only mandatory field.
   * - ``username``
     - *(none)*
     - Username for authentication. If omitted, xnatctl falls back to the
       ``XNAT_USER`` environment variable or prompts interactively.
   * - ``password``
     - *(none)*
     - Password for authentication. Storing passwords in the config file is
       discouraged on shared systems -- prefer environment variables or the
       interactive prompt instead.
   * - ``verify_ssl``
     - ``true``
     - Whether to verify TLS certificates when connecting. Set this to ``false``
       only when working with development servers that use self-signed certificates.
       Never disable verification for production servers.
   * - ``timeout``
     - ``21600``
     - HTTP request timeout in seconds. The default of 21600 (6 hours) is
       deliberately generous to accommodate large DICOM transfers. You can lower
       this for faster failure detection on slow or unreliable networks, or raise it
       further if your transfers routinely exceed six hours.
   * - ``default_project``
     - *(none)*
     - Default project ID used as a fallback when you omit the ``-P`` flag. This
       is especially important for the ``-E`` (experiment) flag: when ``-P`` is not
       provided, xnatctl resolves ``-E`` from this profile field, which lets you
       pass experiment labels instead of accession IDs. Without either ``-P`` or
       ``default_project``, the ``-E`` value must be a full accession ID like
       ``XNAT_E00001``.


Environment Variables
---------------------

Environment variables override their corresponding profile values for the current
shell session. They are most useful in CI/CD pipelines, containers, and scripts where
editing a YAML file is impractical.

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Variable
     - Description
   * - ``XNAT_URL``
     - Server URL. When set, xnatctl creates (or overrides) a ``default`` profile
       at runtime with this URL. Use this in CI pipelines where you inject the
       server address from a secret store.
   * - ``XNAT_USER``
     - Username for authentication. Overrides the ``username`` field in the active
       profile. Pair with ``XNAT_PASS`` for non-interactive login in scripts.
   * - ``XNAT_PASS``
     - Password for authentication. Overrides the ``password`` field in the active
       profile. Always source this from a secret manager or vault rather than
       hard-coding it in a script.
   * - ``XNAT_TOKEN``
     - Pre-existing JSESSION token. When set, xnatctl skips the login handshake
       entirely and uses this token directly. Takes the highest auth priority --
       above cached sessions, credentials, and environment user/password. Use this
       when another system has already authenticated and passes the token downstream.
   * - ``XNAT_PROFILE``
     - Active profile name. Overrides the ``default_profile`` value in
       ``config.yaml`` for the current session. Handy when you want to pin a
       particular profile in a shell without editing the config file.
   * - ``XNAT_VERIFY_SSL``
     - Override SSL verification (``true`` or ``false``). Applied when ``XNAT_URL``
       is also set. Useful for CI environments connecting to development servers
       with self-signed certificates.
   * - ``XNAT_TIMEOUT``
     - Override HTTP timeout in seconds. Applied when ``XNAT_URL`` is also set.
       Use this to tighten the timeout in CI where you want fast failure on
       network issues.

The following example shows a typical CI/CD setup that authenticates with environment
variables and lists session IDs for a project:

.. code-block:: console

   $ export XNAT_URL=https://xnat.example.org
   $ export XNAT_USER=ci-bot
   $ export XNAT_PASS="${XNAT_CI_PASSWORD}"
   $ xnatctl session list -P MYPROJECT --quiet


Credential Priority
-------------------

When xnatctl needs credentials, it checks four sources in order and uses the first
match it finds:

1. **CLI arguments** -- ``--username`` and ``--password`` passed directly on the command
   line.
2. **Environment variables** -- ``XNAT_USER`` and ``XNAT_PASS`` in the current shell.
3. **Profile configuration** -- ``username`` and ``password`` fields in the active
   profile inside ``config.yaml``.
4. **Interactive prompt** -- if none of the above provide credentials, xnatctl asks you
   at the terminal.

.. note::

   This priority chain means you can store a default username in your profile for
   day-to-day use, override it with an environment variable in CI, and still pass
   ``--username`` on the command line when you need to authenticate as a different user
   for a single command. Each layer shadows the ones below it without removing them.


Authentication Flow
-------------------

Before you can run most commands, you need an active session with your XNAT server.
The ``auth login`` command handles the full authentication handshake: it sends your
credentials to the XNAT REST API and receives a JSESSION token in return.

.. code-block:: console

   $ xnatctl auth login

xnatctl caches the resulting session token at ``~/.config/xnatctl/.session`` with
file permissions set to ``0600`` (owner read/write only). The cache stores the token,
the server URL, the username, and an expiry timestamp. XNAT sessions expire after
15 minutes of inactivity by default, and xnatctl respects this window -- once the
cached token passes its expiry time, it is discarded automatically.

If a command encounters an expired session, the ``@require_auth`` decorator
re-authenticates transparently using your stored credentials or environment variables.
You do not need to run ``auth login`` again manually in most cases.

To verify that your session is active and confirm which user you are authenticated as,
use the ``whoami`` command:

.. code-block:: console

   $ xnatctl whoami

You can also explicitly clear your session at any time:

.. code-block:: console

   $ xnatctl auth logout

.. warning::

   Avoid storing passwords directly in ``config.yaml`` on shared or multi-user systems.
   The config file is not encrypted, and anyone with read access to your home directory
   can see the credentials. Prefer environment variables, a secrets manager, or the
   interactive prompt for sensitive environments.
