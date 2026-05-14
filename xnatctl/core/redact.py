"""URL query-string redaction for secret-shaped keys.

This module exposes a single helper, :func:`redact_url_query`, plus the
canonical set of query-parameter key names that xnatctl treats as
secret-shaped. The helper scans free-form text for ``http(s)://...`` URLs,
parses each URL's query string, and rewrites any value whose key (compared
case-insensitively) is in :data:`SECRET_QUERY_KEYS` to ``***``.

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


def redact_url_query(text: str) -> str:
    """Return ``text`` with secret-shaped query values rewritten to ``***``.

    Finds every ``http(s)://...`` substring in ``text``, parses the query
    string, and replaces the value of any parameter whose key (case
    insensitive) is in :data:`SECRET_QUERY_KEYS` with the literal string
    ``***``. Non-secret keys are preserved exactly. URLs without a query
    string or without any secret-shaped key are returned untouched.

    Args:
        text: Arbitrary string that may embed one or more URLs.

    Returns:
        The input string with secret-shaped query values redacted. If no
        redaction is needed, the input is returned unchanged.
    """
    if "?" not in text or "http" not in text:
        return text

    def _scrub(match: re.Match[str]) -> str:
        """Rewrite a single URL match with redacted secret query values."""
        url = match.group(0)
        trailing = ""
        while url and url[-1] in _TRAILING_PUNCT:
            trailing = url[-1] + trailing
            url = url[:-1]

        scheme, netloc, path, query, fragment = urlsplit(url)
        if not query:
            return url + trailing

        # Walk the query string manually so non-secret values keep their
        # original encoding byte-for-byte. We only compare the decoded key
        # name (case-insensitively) against the allowlist.
        changed = False
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

        new_query = "&".join(new_parts)
        return urlunsplit((scheme, netloc, path, new_query, fragment)) + trailing

    return _URL_RE.sub(_scrub, text)
