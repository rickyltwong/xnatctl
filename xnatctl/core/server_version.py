"""XNAT server-version detection and feature-floor gating.

``XNATClient.server_version`` probes the same ``buildInfo`` endpoint
:meth:`XNATClient.ping` uses, parses it tolerantly, and caches the result
(including a failed probe) for the client's lifetime -- see
:func:`probe_server_version`. :func:`require_server_version` is the gate
version-sensitive call sites use: it raises an actionable error when the
version is known and below a feature's floor, and fails open (proceeds,
logging at DEBUG) when the version could not be determined. Version
detection is best-effort diagnostics, not a precondition for using the
client -- it must never turn a working command into a failure, and it must
never make a working command slower: the probe carries its own short
timeout independent of the client's (often multi-hour) transfer timeout,
parsing is total (never raises), and a failed probe is cached so a broken
endpoint is asked at most once per client.

Per-feature floors are named constants here so this module and
``docs/xnat-compatibility.rst`` stay in sync.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from xnatctl.core.exceptions import UnsupportedServerVersionError, XNATCtlError

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

logger = logging.getLogger(__name__)

#: (major, minor, patch) -- the trailing XNAT build number (e.g. the ``.1``
#: in ``1.9.2.1``) is not part of this tuple; see :func:`parse_server_version`.
ServerVersion = tuple[int, int, int]

#: Read-timeout ceiling for the version probe alone. Deliberately short and
#: independent of the client's configured timeout (often the 6-hour transfer
#: default): a stalled buildInfo endpoint must fail in seconds, not hang an
#: upload that hasn't even started yet. Paired with ``max_retries=0`` at the
#: call site -- a short timeout alone does not bound the call, because the
#: retry ladder sleeps on Retry-After between attempts.
PROBE_TIMEOUT_SECONDS = 5

# Anchored to the entire (already-stripped) response body via `fullmatch`,
# not searched for anywhere inside it -- a `.search()` over arbitrary text
# treats any embedded "N.N" pair as a version, so a garbage or unrelated body
# such as an "HTTP/1.1 404 Not Found" page (e.g. from a proxy in front of a
# misconfigured buildInfo endpoint) would parse as XNAT "1.1.0" and then
# wrongly block a perfectly good upload. Each numeric component is capped at
# 4 digits, and each dash/dot-delimited suffix token (the ``-SNAPSHOT``/
# ``.dev0``/trailing-build-number part) is capped at 20 characters: XNAT
# versions and their build suffixes are short, and both caps together keep a
# garbage or adversarial response -- including one that tries to hide an
# oversized digit run inside what looks like a suffix -- from ever reaching
# `int()` with more than a handful of digits. Python's int-from-str
# conversion rejects an oversized digit string past its own limit; excluding
# that shape at the regex means parsing genuinely cannot raise, rather than
# merely being unlikely to.
_VERSION_PREFIX = r"(?:[A-Za-z][A-Za-z ]*\s+)?"
_VERSION_SUFFIX = r"(?:[.\-][A-Za-z0-9]{1,20})*"
_VERSION_RE = re.compile(
    rf"{_VERSION_PREFIX}(\d{{1,4}})\.(\d{{1,4}})(?:\.(\d{{1,4}}))?{_VERSION_SUFFIX}"
)

# =============================================================================
# Feature floors
# =============================================================================

#: Direct-to-archive uploads (``Direct-Archive=true`` on the import service)
#: require XNAT 1.8.3+ -- see docs/xnat-compatibility.rst and the caveat in
#: ``services/import_service.archive_destination_params``.
MIN_VERSION_DIRECT_ARCHIVE: ServerVersion = (1, 8, 3)


def parse_server_version(raw: str | None) -> ServerVersion | None:
    """Tolerantly parse an XNAT build-version string into (major, minor, patch).

    Accepts a plain release string (``"1.8.3"``), a four-component build
    string (``"1.9.2.1"`` -- the trailing build number is dropped), and a
    version embedded in surrounding text such as a snapshot/dev suffix
    (``"1.9.3-SNAPSHOT"``, ``"1.9.3.dev0"``) or a vendor prefix
    (``"Enterprise 1.8.7"``). A missing patch component defaults to 0
    (``"1.9"`` -> ``(1, 9, 0)``). The match is anchored to the whole
    (stripped) string, not searched for within it -- see the module-level
    comment on :data:`_VERSION_RE` for why.

    Returns:
        The parsed ``(major, minor, patch)``, or ``None`` when ``raw`` is
        empty/``None`` or does not consist entirely of a recognizable
        version shape (e.g. ``"unknown"``, or noise such as an HTTP status
        line that merely contains a dotted number pair). ``None`` is the
        "could not determine" signal used throughout this module -- this
        function is total and never raises.
    """
    if not raw:
        return None
    match = _VERSION_RE.fullmatch(raw)
    if match is None:
        return None
    major, minor, patch = match.groups()
    try:
        return (int(major), int(minor), int(patch) if patch is not None else 0)
    except ValueError:
        # Unreachable given _VERSION_RE's per-component digit cap; kept so
        # this function is provably total rather than relying on the regex
        # alone to guarantee it.
        return None


def probe_server_version(client: XNATClient) -> ServerVersion | None:
    """Fetch and parse the server's version from the buildInfo endpoint.

    Called once per client by :attr:`XNATClient.server_version`, which does
    the caching; this function always performs the HTTP call, on its own
    short :data:`PROBE_TIMEOUT_SECONDS` timeout rather than the client's
    configured (often multi-hour) transfer timeout. Any failure -- network,
    auth, a timeout, an unparsable body -- is swallowed and reported as
    ``None`` rather than raised, because a broken or unreachable version
    probe must never break, or slow, a command that does not otherwise need
    it.

    Args:
        client: Bound XNAT client to probe.

    Returns:
        The parsed version, or ``None`` on any HTTP, timeout, or parse
        failure.
    """
    try:
        # Single-shot, not just short-timeout. The retry ladder honours
        # Retry-After up to 300s per attempt, so a 503-ing buildInfo endpoint
        # would otherwise stall an upload for many minutes behind a probe
        # whose entire contract is to be cheap and skippable.
        resp = client.get(
            "/xapi/siteConfig/buildInfo/version",
            timeout=PROBE_TIMEOUT_SECONDS,
            max_retries=0,
        )
        return parse_server_version(resp.text.strip())
    except XNATCtlError as e:
        logger.debug("Could not probe server version: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 -- version probing must never break a working command
        logger.debug("Could not probe server version: %s", e)
        return None


def require_server_version(
    client: XNATClient,
    minimum: ServerVersion,
    feature_name: str,
) -> None:
    """Raise if the server is known to be older than ``minimum``; else proceed.

    Fails open on an unknown version (unreachable/unparsable buildInfo
    endpoint): logs a DEBUG line and returns, rather than blocking an
    operation on a version guess. Only a *known* version below the floor is
    fatal.

    Args:
        client: Bound XNAT client (its ``server_version`` is probed/cached
            lazily on first use).
        minimum: Floor version for ``feature_name``, e.g.
            :data:`MIN_VERSION_DIRECT_ARCHIVE`.
        feature_name: Short subject naming the gated feature for the error
            message, e.g. ``"direct-archive"``.

    Raises:
        UnsupportedServerVersionError: If the server's known version is
            below ``minimum``.
    """
    version = client.server_version
    if version is None:
        logger.debug("Server version unknown; proceeding without gating %s", feature_name)
        return
    if version < minimum:
        raise UnsupportedServerVersionError(feature_name, minimum, version)
