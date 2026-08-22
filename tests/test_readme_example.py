"""README.md's "Use as a Python library" section stays runnable.

The quickstart and exception-handling code blocks are parsed straight out of
README.md at test time -- not retyped here -- so a rename, signature change,
or exception rename in the real API makes the README wrong *and* fails this
test, rather than the two silently drifting apart. The idiom: read the real
doc, don't hand-copy it.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest

import xnatctl
from xnatctl.core.exceptions import SessionExpiredError

_README = Path(__file__).resolve().parent.parent / "README.md"
_SECTION_HEADING = "Use as a Python library"


def _library_section() -> str:
    text = _README.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(_SECTION_HEADING)}\n(.*?)(?=\n## |\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert match, f"README.md is missing the '{_SECTION_HEADING}' section"
    return match.group(1)


def _python_code_blocks(section: str) -> list[str]:
    blocks = re.findall(r"```python\n(.*?)```", section, re.DOTALL)
    assert len(blocks) >= 2, (
        f"expected a quickstart and an exception-handling code block in "
        f"'{_SECTION_HEADING}', found {len(blocks)}"
    )
    return blocks


@pytest.fixture
def _readme_code_blocks() -> list[str]:
    return _python_code_blocks(_library_section())


def _zip_bytes(filename: str, content: bytes) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


def _mock_download_transport() -> httpx.MockTransport:
    """A transport that serves whatever ZIP download_resource() asks for."""
    body = _zip_bytes("scan1/IM001.dcm", b"fake dicom bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/files")
        return httpx.Response(200, content=body, headers={"content-length": str(len(body))})

    return httpx.MockTransport(handler)


def test_readme_quickstart_runs_against_a_mocked_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _readme_code_blocks: list[str],
) -> None:
    """The exact quickstart block from README.md downloads and extracts.

    ``XNATClient.from_profile`` is intercepted at its one function-local
    import seam (``xnatctl.core.connect.build_client_from_profile``) so the
    snippet still calls the real classmethod, gets a real ``XNATClient``, and
    exercises the real ``client.downloads.download_resource`` -- only the
    wire is faked, via ``httpx.MockTransport``, matching how the rest of this
    suite tests wire behavior.
    """
    quickstart = _readme_code_blocks[0]
    assert "XNATClient.from_profile" in quickstart
    assert "download_resource" in quickstart

    fake_client = xnatctl.XNATClient(
        base_url="https://xnat.example.org",
        session_token="cached-session-token",  # skip login on __enter__
        transport=_mock_download_transport(),
    )

    monkeypatch.setattr(
        "xnatctl.core.connect.build_client_from_profile",
        lambda name, *, config_path=None, **_kw: fake_client,
    )
    # The snippet writes to the relative path "./data" -- run it inside
    # tmp_path so that's where the download actually lands.
    monkeypatch.chdir(tmp_path)

    exec(compile(quickstart, "README.md:quickstart", "exec"), {})  # noqa: S102

    extracted = tmp_path / "data" / "DICOM" / "scan1" / "IM001.dcm"
    assert extracted.read_bytes() == b"fake dicom bytes"


def test_readme_exception_example_catches_the_real_exception(
    monkeypatch: pytest.MonkeyPatch,
    _readme_code_blocks: list[str],
) -> None:
    """The except clause names a real, importable xnatctl exception.

    ``client.projects.get`` is made to raise the exact exception
    ``SessionExpiredError`` that download/upload paths raise on an expired
    session, and the snippet's ``except xnatctl.SessionExpiredError`` must
    actually catch it -- proving the name in README.md still resolves to the
    class the library really raises, not just that it parses.
    """
    exception_example = _readme_code_blocks[1]
    assert "except xnatctl." in exception_example

    client = xnatctl.XNATClient(base_url="https://xnat.example.org", session_token="tok")
    monkeypatch.setattr(
        client.projects,
        "get",
        lambda *_a, **_kw: (_ for _ in ()).throw(SessionExpiredError("session expired")),
    )

    exec(  # noqa: S102
        compile(exception_example, "README.md:exception-example", "exec"),
        {"xnatctl": xnatctl, "client": client},
    )
