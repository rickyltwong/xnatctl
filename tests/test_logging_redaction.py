from __future__ import annotations

import io
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


@pytest.fixture
def clean_root() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = {id(h): list(h.filters) for h in saved_handlers}
    saved_level = root.level
    try:
        yield
    finally:
        for handler in root.handlers:
            if id(handler) in saved_filters:
                handler.filters = saved_filters[id(handler)]
        root.handlers = saved_handlers
        root.setLevel(saved_level)


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{'JSESSIONID': 'ABCDEF0123', 'x': '1'}", "{'JSESSIONID': '***', 'x': '1'}"),
        ("""{'JSESSIONID': "ABC'DEF"}""", """{'JSESSIONID': "***"}"""),
        ("{b'JSESSIONID': b'ABCDEF0123'}", "{b'JSESSIONID': b'***'}"),
        ("""{'JSESSIONID': 'AB\\'C"D', 'x': 1}""", "{'JSESSIONID': '***', 'x': 1}"),
        ('{"jsessionid": "abcdef0123"}', '{"jsessionid": "***"}'),
        ("{'JSESSIONID': 'ABCDEF0123", "{'JSESSIONID': '***'"),
        (
            repr(b"{'JSESSIONID': 'ABCDEF0123', 'x': \"s\"}"),
            """b'{\\'JSESSIONID\\': \\'***\\', \\'x\\': "s"}'""",
        ),
        (
            '{\\"JSESSIONID\\": \\"ABCDEF0123\\", \\"x\\": 1}',
            '{\\"JSESSIONID\\": \\"***\\", \\"x\\": 1}',
        ),
        ("{\\'JSESSIONID\\': \\'ABCDEF0123", "{\\'JSESSIONID\\': \\'***\\'"),
        (
            repr(b"{'JSESSIONID': 'ABC\\'SECRET', 'x': \"s\"}"),
            """b'{\\'JSESSIONID\\': \\'***\\', \\'x\\': "s"}'""",
        ),
        (
            repr(repr(b"{'JSESSIONID': 'ABC\\'SECRET', 'x': \"s\"}")),
            repr("""b'{\\'JSESSIONID\\': \\'***\\', \\'x\\': "s"}'"""),
        ),
    ],
    ids=[
        "str",
        "double-quoted",
        "bytes",
        "escaped-quote",
        "json",
        "truncated",
        "inside-a-bytes-repr",
        "json-inside-json",
        "inside-a-repr-truncated",
        "escaped-quote-inside-a-bytes-repr",
        "escaped-quote-two-reprs-deep",
    ],
)
def test_a_session_cookie_in_a_mapping_repr_is_redacted(text: str, expected: str) -> None:
    assert redact_session_cookies(text) == expected


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
