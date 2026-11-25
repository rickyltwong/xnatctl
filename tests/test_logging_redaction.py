from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Iterator

import pytest

from xnatctl.core.exceptions import InvalidURLError
from xnatctl.core.logging import RedactionFilter, install_redaction_filter, setup_logging
from xnatctl.core.redact import redact_session_cookies, redact_url_query
from xnatctl.core.validation import validate_server_url

SECRET_URL = "https://xnat.example.org/data?token=s3cret"
USERINFO_URL = "https://admin:s3cret@xnat.example.org/data"


@pytest.fixture
def filtered_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("xnatctl.test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    install_redaction_filter(logger)
    try:
        yield logger, stream
    finally:
        logger.handlers = []


def test_query_token_is_redacted_in_emitted_output(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.warning("GET %s failed", SECRET_URL)

    output = stream.getvalue()
    assert "s3cret" not in output
    assert "token=***" in output


def test_userinfo_password_is_redacted_in_emitted_output(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.error("Connection to %s refused", USERINFO_URL)

    output = stream.getvalue()
    assert "s3cret" not in output
    assert "admin:***@" in output


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see http://[x?token=s3cret&a=1 there", "see http://[x?token=***&a=1 there"),
        ("http://admin:s3cret@[x?pwd=hunter2", "http://admin:***@[x?pwd=***"),
        ("http://[x/path&x?token=s3cret", "http://[x/path&x?token=***"),
        ("http://u:s3cret@[x?token=pre@hunter2", "http://u:***@[x?token=***"),
    ],
    ids=["query", "userinfo-and-query", "ampersand-in-path", "at-sign-in-query"],
)
def test_a_url_urlsplit_rejects_is_still_redacted_without_raising(
    filtered_logger: tuple[logging.Logger, io.StringIO], text: str, expected: str
) -> None:
    logger, stream = filtered_logger

    logger.info("request %s", text)

    assert stream.getvalue() == f"request {expected}\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTPS://dest.example/?token=s3cret", "HTTPS://dest.example/?token=***"),
        ("Http://admin:s3cret@dest.example/x", "Http://admin:***@dest.example/x"),
    ],
    ids=["uppercase-query", "mixed-case-userinfo"],
)
def test_url_redaction_ignores_the_scheme_case(text: str, expected: str) -> None:
    assert redact_url_query(text) == expected


_TOKEN = "0123456789ABCDEF0123456789ABCDEF"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Cookie: JSESSIONID={_TOKEN}; Path=/", "Cookie: JSESSIONID=***; Path=/"),
        (f"JSESSIONID={_TOKEN}.node1", "JSESSIONID=***"),
        (repr({"JSESSIONID": _TOKEN, "x": "1"}), "{'JSESSIONID': '***', 'x': '1'}"),
        (repr({b"JSESSIONID": _TOKEN.encode()}), "{b'JSESSIONID': b'***'}"),
        (json.dumps({"jsessionid": _TOKEN}), '{"jsessionid": "***"}'),
        ("{'JSESSIONID': '" + _TOKEN[:10], "{'JSESSIONID': '***"),
        (repr(str({"JSESSIONID": _TOKEN}).encode()), """b"{'JSESSIONID': '***'}\""""),
        (repr(json.dumps({"JSESSIONID": _TOKEN})), """'{"JSESSIONID": "***"}'"""),
        (
            repr(repr(str({"JSESSIONID": _TOKEN}).encode())),
            repr("""b"{'JSESSIONID': '***'}\""""),
        ),
        (
            json.dumps(f'Set-Cookie: JSESSIONID="{_TOKEN}"; Path=/'),
            '"Set-Cookie: JSESSIONID=\\"***\\"; Path=/"',
        ),
        (
            repr({"JSESSIONID": "AB\\"}) + " " + repr({"JSESSIONID": _TOKEN}),
            "{'JSESSIONID': '***\\\\'} {'JSESSIONID': '***'}",
        ),
        (
            repr(repr({b"JSESSIONID": _TOKEN.encode(), b"other": b'"'})),
            repr(repr({b"JSESSIONID": b"***", b"other": b'"'})),
        ),
        (
            repr((b"Set-Cookie", b"JSESSIONID=\t " + _TOKEN.encode() + b"; Path=/")),
            "(b'Set-Cookie', b'JSESSIONID=\\t ***; Path=/')",
        ),
        (
            repr("a=b\r\nJSESSIONID=" + _TOKEN + "; Path=/"),
            "'a=b\\r\\nJSESSIONID=***; Path=/'",
        ),
        ("Cookie: a=b%3BJSESSIONID=" + _TOKEN, "Cookie: a=b%3BJSESSIONID=***"),
        (
            "Set-Cookie: XNAT_JSESSIONID=" + _TOKEN + "; Path=/",
            "Set-Cookie: XNAT_JSESSIONID=***; Path=/",
        ),
        ("Cookie: proxy_jsessionid=" + _TOKEN, "Cookie: proxy_jsessionid=***"),
    ],
    ids=[
        "header",
        "with-a-route",
        "mapping",
        "bytes-mapping",
        "json",
        "truncated",
        "inside-a-bytes-repr",
        "json-inside-a-repr",
        "two-reprs-deep",
        "json-escaped-header",
        "after-a-value-ending-in-a-backslash",
        "bytes-mapping-two-reprs-deep",
        "escaped-whitespace-before-the-value",
        "after-an-escaped-newline",
        "after-an-encoded-separator",
        "prefixed-name-in-a-header",
        "prefixed-name-in-a-cookie-header",
    ],
)
def test_a_session_cookie_is_redacted_however_it_is_quoted(text: str, expected: str) -> None:
    assert redact_session_cookies(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "the JSESSIONID cookie expired",
        "XNAT JSESSIONID: refresh required",
        "{'previous_JSESSIONID': 'green'}",
        "{'previous-JSESSIONID': 'green'}",
        "{'oldJSESSIONID': 'green'}",
        "'JSESSIONID: refresh'",
        'Cookie: JSESSIONID="" status=204',
        '{"JSESSIONID":""}',
        '{"my\'JSESSIONID":"green"}',
        "name='JSESSIONID', value=''",
        'JSESSIONID=\\"\\" ok',
        "GET https://xnat.example.org/'JSESSIONID',value=public/data",
    ],
    ids=[
        "prose",
        "prose-with-a-colon",
        "longer-name",
        "hyphenated-name",
        "run-on-name",
        "quoted-prose",
        "empty-header-value",
        "empty-json-value",
        "mismatched-quotes",
        "empty-cookie-jar-value",
        "escaped-empty-value",
        "url-path-with-the-name",
    ],
)
def test_text_that_only_mentions_the_cookie_is_kept(text: str) -> None:
    assert redact_session_cookies(text) == text


@pytest.mark.parametrize(
    "text",
    [
        repr(repr(b"JSESSIONID=\t " + _TOKEN.encode() + b"; Path=/")),
        repr(repr(repr(b"JSESSIONID=\t\n " + _TOKEN.encode()))),
        repr('{"JSESSIONID"\t: "' + _TOKEN + '"}'),
        '"Set-Cookie: JSESSIONID\\u0009=' + _TOKEN + '; Path=/"',
        repr('"Set-Cookie: JSESSIONID\\u0009=' + _TOKEN + '; Path=/"'),
        repr((b"Set-Cookie", b'JSESSIONID="\t' + _TOKEN.encode() + b'"; Path=/')),
    ],
    ids=[
        "escaped-tab-two-reprs-deep",
        "escaped-tab-three-reprs-deep",
        "escaped-tab-before-a-colon",
        "json-unicode-tab",
        "json-unicode-tab-in-a-repr",
        "escaped-tab-inside-the-quotes",
    ],
)
def test_a_token_behind_escaped_whitespace_is_redacted(text: str) -> None:
    redacted = redact_session_cookies(text)

    assert _TOKEN not in redacted
    assert "***" in redacted


def test_a_cookie_jar_repr_is_redacted() -> None:
    import httpx

    response = httpx.Response(
        200,
        headers=[("Set-Cookie", f"JSESSIONID={_TOKEN}; Path=/")],
        request=httpx.Request("GET", "https://xnat.example.org/data"),
    )
    jar = response.cookies.jar

    for text in (repr(jar), repr(list(jar)), repr(jar._cookies), repr(repr(list(jar)))):
        assert _TOKEN not in redact_session_cookies(text), text


def test_a_quoted_cookie_value_is_redacted_in_json() -> None:
    import httpx

    response = httpx.Response(
        200,
        headers=[("Set-Cookie", f'JSESSIONID="{_TOKEN}"; Path=/')],
        request=httpx.Request("GET", "https://xnat.example.org/data"),
    )
    cookies = dict(response.cookies)

    for text in (json.dumps(cookies), repr(json.dumps(cookies)), json.dumps(json.dumps(cookies))):
        assert _TOKEN not in redact_session_cookies(text), text


def test_a_session_cookie_value_ends_at_a_quote_or_backslash() -> None:
    # Server session ids never hold either, so the tail of a made-up one that
    # does stays visible: the matcher does not parse quoting.
    assert (
        redact_session_cookies("""{'JSESSIONID': "ABC'DEF"}""") == """{'JSESSIONID': "***'DEF"}"""
    )


def test_session_cookie_redaction_stays_linear_on_long_backslash_runs() -> None:
    text = "JSESSIONID " + "\\" * 200_000

    started = time.perf_counter()
    redact_session_cookies(text)

    assert time.perf_counter() - started < 1.0


def test_redaction_stays_linear_on_long_unparseable_urls() -> None:
    text = "http://[x" + "?" * 200_000

    started = time.perf_counter()
    assert redact_url_query(text) == text
    assert time.perf_counter() - started < 1.0


def test_redaction_stays_linear_on_long_trailing_punctuation() -> None:
    text = "https://h/p?token=s3cret" + ")" * 200_000

    started = time.perf_counter()
    redacted = redact_url_query(text)
    assert time.perf_counter() - started < 1.0
    assert redacted == "https://h/p?token=***" + ")" * 200_000


def test_a_record_whose_char_argument_overflows_is_still_emitted(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger

    logger.info("code point %c", 10**10)

    assert "code point %c" in stream.getvalue()


def test_a_record_whose_args_do_not_fit_its_format_is_still_emitted(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger

    logger.info("got %d from %s", "not-a-number", SECRET_URL)

    output = stream.getvalue()
    assert "got %d from %s" in output
    assert "s3cret" not in output


@pytest.mark.parametrize(
    ("msg", "args"),
    [("%(missing)s from %(url)s", ({"url": SECRET_URL},)), ("bad %y from %s", (1, SECRET_URL))],
    ids=["missing-key", "unknown-conversion"],
)
def test_a_record_with_a_broken_format_is_still_emitted_and_redacted(
    filtered_logger: tuple[logging.Logger, io.StringIO], msg: str, args: tuple[object, ...]
) -> None:
    logger, stream = filtered_logger

    logger.info(msg, *args)

    output = stream.getvalue()
    assert msg in output
    assert "s3cret" not in output


@pytest.mark.parametrize(
    "msg",
    ["Cookie: JSESSIONID=%s %s", "GET https://host/data?token=%s %s"],
    ids=["cookie", "url-query"],
)
def test_arguments_that_do_not_fit_the_format_are_not_shown(
    filtered_logger: tuple[logging.Logger, io.StringIO], msg: str
) -> None:
    logger, stream = filtered_logger

    logger.warning(msg, _TOKEN)

    output = stream.getvalue()
    assert _TOKEN not in output
    assert "arguments not shown: str" in output


def test_a_mapping_argument_is_not_read_when_the_format_does_not_fit(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    class Unreadable(dict[str, object]):
        def values(self) -> object:
            raise OSError("not readable")

    logger, stream = filtered_logger

    logger.warning("%(missing)s", Unreadable(present=1))

    assert "%(missing)s [arguments not shown: Unreadable]" in stream.getvalue()


def test_records_without_secrets_are_left_untouched(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.info("Uploaded %d files to %s", 3, "https://xnat.example.org/data")

    assert stream.getvalue().strip() == "Uploaded 3 files to https://xnat.example.org/data"


def test_untouched_record_keeps_lazy_args() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "plain %s", ("value",), None)
    assert RedactionFilter().filter(record) is True
    assert record.msg == "plain %s"
    assert record.args == ("value",)


def test_changed_record_is_collapsed_without_double_interpolation() -> None:
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "GET %s", (SECRET_URL,), None)
    assert RedactionFilter().filter(record) is True
    assert record.args is None
    assert "s3cret" not in record.getMessage()
    assert record.getMessage() == record.getMessage()


def test_third_party_logger_output_is_redacted_too() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    httpx_logger = logging.getLogger("httpx")
    saved, saved_level = list(httpx_logger.handlers), httpx_logger.level
    httpx_logger.handlers = [handler]
    httpx_logger.propagate = False
    httpx_logger.setLevel(logging.INFO)
    install_redaction_filter(httpx_logger)
    try:
        httpx_logger.info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"',
            "https://xnat.example.org/data/projects?token=s3cret&format=json",
        )
    finally:
        httpx_logger.handlers, httpx_logger.propagate = saved, True
        httpx_logger.setLevel(saved_level)

    output = stream.getvalue()
    assert "s3cret" not in output
    assert "token=***" in output
    assert "format=json" in output


@pytest.mark.usefixtures("clean_root")
def test_setup_logging_installs_the_filter_exactly_once() -> None:
    setup_logging()
    setup_logging()
    setup_logging(verbose=True)

    root = logging.getLogger()
    assert root.handlers, "setup_logging should leave the root logger with a handler"
    for handler in root.handlers:
        installed = [f for f in handler.filters if isinstance(f, RedactionFilter)]
        assert len(installed) <= 1


def test_userinfo_url_is_rejected() -> None:
    with pytest.raises(InvalidURLError) as exc_info:
        validate_server_url(USERINFO_URL)

    assert "Do not embed credentials" in str(exc_info.value)


def test_rejection_message_does_not_leak_the_password() -> None:
    with pytest.raises(InvalidURLError) as exc_info:
        validate_server_url(USERINFO_URL)

    error = exc_info.value
    assert "s3cret" not in str(error)
    assert "s3cret" not in str(error.url)
    assert "s3cret" not in str(error.details)


def test_password_is_not_leaked_by_an_unrelated_rejection() -> None:
    with pytest.raises(InvalidURLError) as exc_info:
        validate_server_url("ftp://admin:s3cret@xnat.example.org")

    assert "s3cret" not in str(exc_info.value)
    assert "Unsupported scheme" in str(exc_info.value)


def test_bare_username_url_is_still_rejected() -> None:
    with pytest.raises(InvalidURLError):
        validate_server_url("https://admin@xnat.example.org")


def test_ordinary_urls_still_validate() -> None:
    assert validate_server_url("https://xnat.example.org/") == "https://xnat.example.org"
    assert validate_server_url("http://localhost:8080") == "http://localhost:8080"
