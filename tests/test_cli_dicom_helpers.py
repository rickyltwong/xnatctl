from __future__ import annotations

from pathlib import Path

import pytest

from xnatctl.cli.dicom_cmd import _anonymize_targets, _is_empty_required_value


@pytest.mark.parametrize(
    ("value", "empty"),
    [
        (None, True),
        (b"", True),
        (bytearray(), True),
        (b"x", False),
        ("  ", True),
        ("PID01", False),
    ],
    ids=["none", "empty-bytes", "empty-bytearray", "bytes", "blank-str", "str"],
)
def test_is_empty_required_value(value: object, empty: bool) -> None:
    assert _is_empty_required_value(value) is empty


@pytest.fixture
def input_tree(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    (src / "sub").mkdir(parents=True)
    (src / "top.dcm").write_bytes(b"")
    (src / "sub" / "deep.dcm").write_bytes(b"")
    return src


def test_anonymize_targets_recursive_keeps_relative_layout(
    input_tree: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"

    targets = _anonymize_targets(input_tree, out, recursive=True)

    assert sorted(targets) == sorted(
        [
            (input_tree / "top.dcm", out / "top.dcm"),
            (input_tree / "sub" / "deep.dcm", out / "sub" / "deep.dcm"),
        ]
    )


def test_anonymize_targets_flat_takes_only_top_level_files(
    input_tree: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"

    assert _anonymize_targets(input_tree, out, recursive=False) == [
        (input_tree / "top.dcm", out / "top.dcm")
    ]


def test_anonymize_targets_single_file_maps_to_the_output_path(
    input_tree: Path, tmp_path: Path
) -> None:
    src = input_tree / "top.dcm"
    out = tmp_path / "anon.dcm"

    assert _anonymize_targets(src, out, recursive=True) == [(src, out)]
