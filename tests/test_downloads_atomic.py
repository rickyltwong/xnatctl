"""Atomicity and Content-Length verification for stream_to_file.

The helper must never leave a truncated final file or a stray ``.part`` behind:
a size mismatch or a mid-stream transport error has to fail loudly with neither
artefact on disk.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import DownloadError, NetworkError
from xnatctl.services.downloads import stream_to_file

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> XNATClient:
    client = XNATClient(base_url="https://xnat.example.org", session_token="TOKEN", max_retries=0)
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    return client


def _no_part_files(directory: Path) -> bool:
    return not list(directory.glob("*.part"))


def test_matching_content_length_writes_final_file(tmp_path: Path) -> None:
    payload = b"a complete payload"
    client = _client(
        lambda r: httpx.Response(
            200, content=payload, headers={"content-length": str(len(payload))}
        )
    )
    dest = tmp_path / "out.zip"

    result = stream_to_file(client, "/data/files", dest)

    assert dest.exists()
    assert dest.read_bytes() == payload
    assert result.bytes_written == len(payload)
    assert result.content_length == len(payload)
    assert _no_part_files(tmp_path)


def test_content_length_mismatch_raises_and_leaves_nothing(tmp_path: Path) -> None:
    # Server lies: declares more bytes than it sends.
    client = _client(
        lambda r: httpx.Response(200, content=b"short", headers={"content-length": "999"})
    )
    dest = tmp_path / "out.zip"

    with pytest.raises(DownloadError, match="999"):
        stream_to_file(client, "/data/files", dest)

    assert not dest.exists()
    assert _no_part_files(tmp_path)


@pytest.mark.parametrize("bad_length", ["not-a-number", "-5"])
def test_malformed_content_length_is_ignored(tmp_path: Path, bad_length: str) -> None:
    """A garbage Content-Length disables verification instead of failing the download."""
    payload = b"payload"
    client = _client(
        lambda r: httpx.Response(200, content=payload, headers={"content-length": bad_length})
    )
    dest = tmp_path / "out.zip"

    result = stream_to_file(client, "/data/files", dest)

    assert dest.read_bytes() == payload
    assert result.content_length is None


def test_content_encoded_response_skips_length_check(tmp_path: Path) -> None:
    """With non-identity Content-Encoding the header counts wire bytes, not
    the decoded bytes written, so a mismatch there is not corruption.
    """
    import gzip

    payload = b"decoded payload bytes"
    compressed = gzip.compress(payload)
    client = _client(
        lambda r: httpx.Response(
            200,
            content=compressed,
            headers={"content-length": str(len(compressed)), "content-encoding": "gzip"},
        )
    )
    dest = tmp_path / "out.zip"

    result = stream_to_file(client, "/data/files", dest)

    assert dest.read_bytes() == payload
    assert result.bytes_written == len(payload)
    assert result.content_length is None


def test_mid_stream_read_error_raises_typed_and_leaves_nothing(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield b"first-chunk"
            raise httpx.ReadError("connection dropped mid-body")

        return httpx.Response(
            200,
            headers={"content-length": "1000"},
            content=body(),
        )

    client = _client(handler)
    dest = tmp_path / "out.zip"

    # A failure once the body is streaming is not retryable; it must surface
    # as the typed NetworkError, never a raw httpx exception.
    with pytest.raises(NetworkError):
        stream_to_file(client, "/data/files", dest)

    assert not dest.exists()
    assert _no_part_files(tmp_path)
