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

After writing the profile, ``config init`` offers to log in right away (on a
real terminal; pass ``--login`` or ``--no-login`` to decide up front in
scripts), so a fresh machine gets from nothing to an authenticated session in
one command.

After running ``config init``, your configuration file will look like this:

.. code-block:: yaml

   # ~/.config/xnatctl/config.yaml
   version: 1                        # Config file schema version (see Config File Versioning below)
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
     - Password for authentication, stored in plaintext. Discouraged: prefer the
       OS keychain (see ``password_source``), environment variables, or the
       interactive prompt. See `Storing Passwords in the OS Keychain`_.
   * - ``password_source``
     - *(none)*
     - Set to ``keyring`` to read the password from the OS keychain instead of
       the inline ``password`` field. Written for you by
       ``xnatctl config set-password``.
   * - ``verify_ssl``
     - ``true``
     - Whether to verify TLS certificates when connecting. Set this to ``false``
       only when working with development servers that use self-signed certificates.
       Never disable verification for production servers.
   * - ``ca_bundle``
     - *(none)*
     - Path to a custom CA bundle for TLS verification -- the secure alternative
       to ``verify_ssl: false`` for servers with self-signed or institutional
       certificates.
   * - ``timeout``
     - ``21600``
     - HTTP read timeout in seconds -- how long a single request may take once
       connected. The default of 21600 (6 hours) is deliberately generous to
       accommodate large DICOM transfers. The *connect* phase is governed
       separately and fails in about 10 seconds regardless of this value, so
       an unreachable or firewalled host errors out quickly instead of
       hanging for hours.
   * - ``default_project``
     - *(none)*
     - Default project ID used as a fallback when you omit the ``-P`` flag. This
       is especially important for the ``-E`` (experiment) flag: when ``-P`` is not
       provided, xnatctl resolves ``-E`` from this profile field, which lets you
       pass experiment labels instead of accession IDs. Without either ``-P`` or
       ``default_project``, the ``-E`` value must be a full accession ID like
       ``XNAT_E00001``.
   * - ``workers``
     - *(none)*
     - Default number of parallel workers for upload, download, and batch
       operations. When set, commands use this value unless ``--workers`` is
       passed explicitly on the command line.
   * - ``overwrite``
     - *(none)*
     - Default overwrite mode for uploads (``none``, ``append``, or ``delete``).
       Overridden by ``--overwrite`` on the command line.
   * - ``direct_archive``
     - *(none)*
     - Whether to use direct archive (``true``) or prearchive (``false``) for
       uploads. Overridden by ``--direct-archive`` / ``--prearchive``.
   * - ``archive_mode``
     - *(none)*
     - Default upload mode (``tar``, ``zip``, or ``gradual``). Overridden by
       ``--mode`` on the command line.
   * - ``extract``
     - *(none)*
     - Whether to extract downloaded ZIPs by default (``true`` or ``false``).
       Overridden by ``--extract`` / ``--no-extract``.


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
     - Override SSL verification. Accepts ``true``/``false``/``1``/``0``/
       ``yes``/``no`` (case-insensitive); any other value is an error rather
       than silently disabling verification. Applied when ``XNAT_URL`` is also
       set. Disabling verification prints a warning -- prefer ``ca_bundle`` in
       the profile for self-signed certificates.
   * - ``XNAT_TIMEOUT``
     - Override HTTP timeout in seconds. Applied when ``XNAT_URL`` is also set.
       Use this to tighten the timeout in CI where you want fast failure on
       network issues.
   * - ``XNATCTL_DEBUG``
     - Set to ``1`` to enable full diagnostics: debug logging plus a complete
       httpx/httpcore wire trace, and a traceback on unexpected errors. Unlike
       ``--verbose`` this is read before flags are parsed, so it also covers
       failures during startup. ``0``, ``false``, ``no`` and ``off`` count as
       unset. See :doc:`debugging`.

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

1. **CLI arguments** -- ``--username`` on the command line, and for the
   password ``--password-stdin`` (reads one line from stdin). A password
   *value* on argv is refused with a usage error: it would be visible in
   ``ps``, ``/proc/*/cmdline``, and shell history.
2. **Environment variables** -- ``XNAT_USER`` and ``XNAT_PASS`` in the current shell.
3. **Profile configuration** -- the ``username`` field plus either the inline
   ``password`` or, with ``password_source: keyring``, the OS keychain.
4. **Interactive prompt** -- if none of the above provide credentials, xnatctl asks you
   at the terminal.

.. note::

   This priority chain means you can store a default username in your profile for
   day-to-day use, override it with an environment variable in CI, and still pass
   ``--username`` on the command line when you need to authenticate as a different user
   for a single command. Each layer shadows the ones below it without removing them.


Storing Passwords in the OS Keychain
------------------------------------

Instead of a plaintext ``password`` in ``config.yaml``, xnatctl can keep the
password in your operating system's keychain (macOS Keychain, GNOME
Keyring/KWallet, Windows Credential Manager) via the `keyring
<https://pypi.org/project/keyring/>`_ package, which ships with every install.

If you already have a profile with an inline plaintext password, migrating
takes one command:

.. code-block:: console

   $ xnatctl config set-password prod
   Password: ********
   Repeat for confirmation: ********
   ✓ Password for profile 'prod' stored in the OS keychain

``set-password`` prompts for the password (never accepts it as an argument),
stores it in the keychain, writes ``password_source: keyring`` into the
profile, and **removes the inline** ``password:`` **line** from
``config.yaml``. Afterwards the profile looks like:

.. code-block:: yaml

   profiles:
     prod:
       url: https://xnat.example.org
       username: admin
       password_source: keyring   # no plaintext password on disk

Nothing else changes: every command resolves the password through the same
credential chain, so ``auth login``, transfers, and automatic
re-authentication all read the keychain transparently. ``XNAT_PASS`` still
wins over the keychain when set.

If the keychain entry is missing, commands fail with an error naming the
exact fix. A failed keychain write leaves ``config.yaml`` untouched.

.. note::

   On headless servers without a keychain backend, use the ``XNAT_PASS``
   environment variable sourced from a secret store instead, or simply delete
   the ``password:`` line and let interactive commands prompt.


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

   Avoid storing passwords directly in ``config.yaml``. The file is written
   ``0600``, but it is not encrypted, and a plaintext password in it is
   readable by anything running as you. Prefer the OS keychain
   (``xnatctl config set-password``), environment variables, a secrets manager,
   or the interactive prompt. xnatctl warns at startup if it finds a plaintext
   password in a config file that other users can read.

Config File Versioning
-----------------------

Both ``config.yaml`` and the session cache (``~/.config/xnatctl/.session``)
carry a ``version`` field. It exists so the on-disk shape can change later
without breaking files written by an older xnatctl:

- A ``config.yaml`` with no ``version`` key predates the field and is
  treated as version 1, same as an explicit ``version: 1`` -- it keeps
  loading exactly as before.
- Loading applies any registered migrations for that file's version, in
  memory only. The file on disk is never rewritten just because it was
  read -- it may be root-owned or shared read-only. The migrated,
  current-version form is written back only the next time a command that
  already mutates the config saves it (``add-profile``, ``remove-profile``,
  ``use-context``, ``set-password``, or ``config init``).
- A file declaring a version newer than this xnatctl understands still
  *loads* best-effort, with a warning naming both versions. It cannot be
  *saved*, though: this build never captured whatever new fields that
  version added, so writing the file back would silently drop them and
  relabel the file as the older, current version. Every mutating command
  refuses with an actionable error instead -- upgrade xnatctl, or edit
  the file by hand -- **except** ``config init --force``, which is the
  deliberate escape hatch: it exists to recover from a config.yaml this
  build cannot even parse, so it cannot itself refuse on a version it CAN
  parse either, or it would be useless for the file it is most needed for.
  It still warns (best-effort -- a genuinely unparseable file gets no
  warning, since there is nothing to inspect) before overwriting a
  readable, newer-version file.
- Unrecognized keys anywhere in the file (top-level or inside a profile)
  produce one warning listing them and are otherwise ignored -- never a
  hard error, so a config edited by a newer xnatctl keeps working with an
  older one.
- The session cache has no version field predating it, and no migration
  table: a missing ``version`` key is simply version 1, and loads
  normally. Any other mismatch (older, newer, or unparseable) discards the
  cached token and falls through to a normal re-authentication instead of
  migrating it. There is no user data at stake in a cache entry -- forcing
  a fresh login is always safe, and simpler than a migration would be.

The rule for future changes: **any change to the config file format ships
a migration function keyed off the version field**, and old files keep
loading rather than erroring out. The session cache format deliberately
does not follow this rule -- see above.

Audit trail
-----------

Destructive commands -- anything that prompts for confirmation, such as
``subject delete``, ``scan delete`` and ``prearchive delete`` -- append one
JSON line to ``~/.config/xnatctl/audit.log``, created ``0600``. Each record
holds the timestamp, command, profile, server, user, the identifiers the
command targeted, whether it was a ``--dry-run``, and the outcome:

.. code-block:: json

   {"timestamp": "2026-07-28T14:02:11-04:00", "operation": "xnatctl subject delete",
    "success": true, "profile": "prod", "server": "https://xnat.example.org",
    "user": "admin", "project": "STUDY01", "subject": "SUB001"}

This answers "who deleted SUB001 from this workstation, when, and against which
server" locally -- XNAT's own audit log is server-side and often not readable
by the person who ran the command. Read-only commands are not recorded, and
neither is a confirmation you declined, since nothing was attempted.

Secrets never enter the file: credential-shaped parameters are dropped by name
and every recorded string is redacted the same way log output is. The log
rotates once to ``audit.log.1`` at 10 MB. Writing is best-effort -- if the file
cannot be written, xnatctl warns and completes the operation anyway.
