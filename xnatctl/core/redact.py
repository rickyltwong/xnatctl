"""URL query-string redaction for secret-shaped keys.

This module exposes :func:`redact_url_query`, plus the canonical set of
query-parameter key names that xnatctl treats as secret-shaped. The helper
scans free-form text for ``http(s)://...`` URLs, parses each URL's query
string, and rewrites any value whose key (compared case-insensitively) is in
:data:`SECRET_QUERY_KEYS` to ``***``. It also scrubs the password out of a
``user:pass@host`` authority -- credentials embedded in a URL would
otherwise ride along into every error message and log line that echoes it.

:func:`redact_url_userinfo` does the authority half alone for a single URL of
any scheme, which validation needs for values that are not http(s) at all.

The function is intentionally conservative: if the input contains no URL,
or no URL has a secret-shaped key, the input is returned unchanged
byte-for-byte. This keeps redaction invisible to any caller whose output
is not actually leaking a secret.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

SECRET_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
    }
)

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_TRAILING_PUNCT = ".,);]"

# Matches the whole `user:pass@` authority prefix after `//`. The inner class
# excludes `/` but not `@`, so it runs greedily to the last `@` of the
# authority -- which is what a password containing `@` requires.
_USERINFO_RE = re.compile(r"(//)([^/\s]*@)")


def _scrub_userinfo(netloc: str) -> tuple[str, bool]:
    """Replace the password in a ``user:pass@host`` netloc with ``***``.

    Returns the (possibly rewritten) netloc and whether anything changed. A
    bare ``user@host`` is left alone: it carries no password, and usernames are
    not treated as secrets here -- the query-string pass deliberately preserves
    ``username=`` for the same reason.
    """
    if "@" not in netloc:
        return netloc, False
    userinfo, _, host = netloc.rpartition("@")
    if ":" not in userinfo:
        return netloc, False
    user = userinfo.partition(":")[0]
    return f"{user}:***@{host}", True


def redact_url_userinfo(url: str) -> str:
    """Return a single ``url`` with any embedded password replaced by ``***``.

    Unlike :func:`redact_url_query` this takes one URL rather than scanning
    free-form text, and it is scheme-agnostic. That matters for validation
    errors: a value rejected by ``validate_server_url`` may well not be an
    http(s) URL, so the text-scanning helper -- which only looks at ``http``
    URLs -- would hand it back with the password intact.
    """

    def _mask(match: re.Match[str]) -> str:
        prefix, userinfo = match.group(1), match.group(2)[:-1]
        if ":" not in userinfo:
            return match.group(0)
        return f"{prefix}{userinfo.partition(':')[0]}:***@"

    return _USERINFO_RE.sub(_mask, url)


def redact_url_query(text: str) -> str:
    """Return ``text`` with secret-shaped URL values rewritten to ``***``.

    Finds every ``http(s)://...`` substring in ``text``, parses the query
    string, and replaces the value of any parameter whose key (case
    insensitive) is in :data:`SECRET_QUERY_KEYS` with the literal string
    ``***``. The password half of a ``user:pass@host`` authority is replaced
    the same way. Non-secret keys, and URLs with neither a secret-shaped key
    nor an embedded password, are preserved exactly.

    Args:
        text: Arbitrary string that may embed one or more URLs.

    Returns:
        The input string with secret-shaped query values redacted. If no
        redaction is needed, the input is returned unchanged.
    """
    # A userinfo URL needs rewriting even with no query string at all, so the
    # cheap bail-out has to admit `@` as well as `?`.
    if "http" not in text or ("?" not in text and "@" not in text):
        return text

    def _scrub(match: re.Match[str]) -> str:
        """Rewrite a single URL match with redacted secrets."""
        url = match.group(0)
        trailing = ""
        while url and url[-1] in _TRAILING_PUNCT:
            trailing = url[-1] + trailing
            url = url[:-1]

        scheme, netloc, path, query, fragment = urlsplit(url)

        netloc, changed = _scrub_userinfo(netloc)

        # Walk the query string manually so non-secret values keep their
        # original encoding byte-for-byte. We only compare the decoded key
        # name (case-insensitively) against the allowlist.
        new_parts: list[str] = []
        for raw_pair in query.split("&"):
            if "=" in raw_pair:
                raw_key, raw_value = raw_pair.split("=", 1)
                decoded_key = unquote(raw_key)
                if decoded_key.lower() in SECRET_QUERY_KEYS and raw_value:
                    new_parts.append(f"{raw_key}=***")
                    changed = True
                else:
                    new_parts.append(raw_pair)
            else:
                new_parts.append(raw_pair)

        if not changed:
            return url + trailing

        return urlunsplit((scheme, netloc, path, "&".join(new_parts), fragment)) + trailing

    return _URL_RE.sub(_scrub, text)
