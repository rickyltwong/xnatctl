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

:func:`redact_session_cookies` rewrites ``JSESSIONID`` cookie values, which
the debug wire trace logs from XNAT's ``Set-Cookie`` headers.

The function is intentionally conservative: if the input contains no URL,
or no URL has a secret-shaped key, the input is returned unchanged
byte-for-byte. This keeps redaction invisible to any caller whose output
is not actually leaking a secret.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

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

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,);]"

_USERINFO_RE = re.compile(r"(//)([^/\s]*@)")


def _scrub_userinfo(netloc: str) -> tuple[str, bool]:
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


def _split_url(url: str) -> tuple[str, str, str, str, str]:
    # Not urlsplit: it raises on text that still looks like a URL, such as an
    # unbalanced "[" host, and redaction runs inside logging calls.
    scheme, _, rest = url.partition("://")
    authority_end = min((i for i in map(rest.find, "/?#") if i != -1), default=len(rest))
    netloc, rest = rest[:authority_end], rest[authority_end:]
    rest, hash_mark, fragment = rest.partition("#")
    path, question_mark, query = rest.partition("?")
    return f"{scheme}://", netloc, path + question_mark, query, hash_mark + fragment


def _scrub_query(query: str) -> tuple[str, bool]:
    changed = False
    new_parts: list[str] = []
    for raw_pair in query.split("&"):
        if "=" in raw_pair:
            raw_key, raw_value = raw_pair.split("=", 1)
            if unquote(raw_key).lower() in SECRET_QUERY_KEYS and raw_value:
                new_parts.append(f"{raw_key}=***")
                changed = True
                continue
        new_parts.append(raw_pair)
    return "&".join(new_parts), changed


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
    if "http" not in text.lower() or ("?" not in text and "@" not in text):
        return text

    def _scrub(match: re.Match[str]) -> str:
        full = match.group(0)
        url = full.rstrip(_TRAILING_PUNCT)
        trailing = full[len(url) :]

        head, netloc, path, query, fragment = _split_url(url)
        netloc, netloc_changed = _scrub_userinfo(netloc)
        query, query_changed = _scrub_query(query)
        if not (netloc_changed or query_changed):
            return url + trailing
        return f"{head}{netloc}{path}{query}{fragment}{trailing}"

    return _URL_RE.sub(_scrub, text)


# A JSESSIONID value is the server's hex session id. Wherever it appears -- a
# Cookie or Set-Cookie header, a mapping's repr, JSON, a cookie jar's
# ``name='JSESSIONID', value='...'``, or any of those escaped inside another
# repr -- it follows the name and a "=", or a ":" or ", value=" after a name
# between matching quotes. Then come spaces, a bytes prefix, the opening quotes
# and whitespace escapes, in that order; it ends at the next quote, backslash,
# whitespace, ";", "," or closing bracket. Only an escape, never literal
# whitespace, may follow the quotes, so an empty value ("" or '') is never
# taken for one. A b is a bytes prefix after a space (``: b'...'``) or before
# a quote and a value (``=b'...'``); otherwise ``=b'`` is a value of b. A space
# is whitespace or a backslash run with an optional whitespace escape after
# it; a whitespace escape is \t, \n or \r, or JSON's four-digit form of tab,
# newline, return or space, at any escaping depth. Each character before the
# value has one way to match, so the scan stays linear. Matching the quotes
# themselves would need a rule per escaping depth.
_ESCAPED_SPACE = r"(?:[tnr]|u00(?:0[9ad]|20))"
_SPACE = r"(?:\\++" + _ESCAPED_SPACE + r"?|\s)"
_SESSION_COOKIE_RE = re.compile(
    r"""
    (
      JSESSIONID
      (?:
        (?:"""
    + _SPACE
    + r"""|['"])*+=
      | (?<=(?P<quote>['"])JSESSIONID)(?=\\*(?P=quote))
        (?:"""
    + _SPACE
    + r"""|['"])*+(?::|,[ ]value=)
      )
      """
    + _SPACE
    + r"""*+
      (?:(?<=\s)b(?=\\*['"])|b(?=\\*['"][^\s\\'";,)\]}]))?+
      (?:\\*+['"])*+
      (?:\\++"""
    + _ESCAPED_SPACE
    + r""")*+
    )
    [^\s\\'";,)\]}]+
    """,
    re.IGNORECASE | re.VERBOSE,
)


def redact_session_cookies(text: str) -> str:
    """Return ``text`` with every ``JSESSIONID`` cookie value rewritten to ``***``.

    ``XNATCTL_DEBUG``'s wire trace logs each response header, XNAT's
    ``Set-Cookie: JSESSIONID=...`` included, and that value is a live session.
    Text without a session cookie is returned unchanged.
    """
    if "jsessionid" not in text.lower():
        return text
    return _SESSION_COOKIE_RE.sub(r"\1***", text)
