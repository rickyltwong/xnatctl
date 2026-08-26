"""Tests for CLI common helpers."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from xnatctl.cli.common import (
    Context,
    _parse_batch_text,
    apply_filter,
    apply_sort_limit,
    batch_option,
    require_auth,
    resolve_columns,
)
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import AuthenticationError


def _protected_command(ctx: Context) -> str:
    return "ok"


def test_require_auth_reauthenticates_on_stale_session():
    ctx = Context()
    ctx.config = Config(profiles={"default": Profile(url="https://example.org")})
    ctx.config.profiles["default"].username = "user"
    ctx.config.profiles["default"].password = "pass"

    class FakeClient:
        base_url = "https://example.org"

        def __init__(self) -> None:
            self.session_token = "old-token"
            self.authenticate_calls = 0

        @property
        def is_authenticated(self) -> bool:
            return self.session_token is not None

        def whoami(self) -> dict[str, str]:
            raise AuthenticationError("https://example.org", "expired")

        def authenticate(self) -> str:
            self.authenticate_calls += 1
            self.session_token = "new-token"
            return "new-token"

    mock_client = FakeClient()

    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()

    decorated = require_auth(_protected_command)
    result = decorated(ctx)

    assert result == "ok"
    ctx.auth_manager.clear_session.assert_called_once_with()
    assert mock_client.authenticate_calls == 1
    ctx.auth_manager.save_session.assert_called_once_with(
        token="new-token",
        url="https://example.org",
        username="user",
    )
    assert mock_client.session_token == "new-token"


def test_require_auth_raises_when_session_expired_and_no_creds(monkeypatch):
    monkeypatch.delenv("XNAT_USER", raising=False)
    monkeypatch.delenv("XNAT_PASS", raising=False)

    ctx = Context()
    ctx.config = Config(profiles={"default": Profile(url="https://example.org")})

    class FakeClient:
        base_url = "https://example.org"

        def __init__(self) -> None:
            self.session_token = "old-token"

        @property
        def is_authenticated(self) -> bool:
            return self.session_token is not None

        def whoami(self) -> dict[str, str]:
            raise AuthenticationError("https://example.org", "expired")

    mock_client = FakeClient()

    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()

    decorated = require_auth(_protected_command)

    with pytest.raises(click.ClickException) as excinfo:
        decorated(ctx)

    message = str(excinfo.value)
    assert "Session expired" in message
    assert "xnatctl auth login" in message


def test_get_client_uses_cached_session_username_as_hint():
    ctx = Context()
    ctx.config = Config(
        default_profile="default",
        profiles={"default": Profile(url="https://example.org", verify_ssl=False)},
    )

    mock_session = MagicMock()
    mock_session.token = "cached-token"
    mock_session.username = "Ricky_Wong"

    ctx.auth_manager = MagicMock()
    ctx.auth_manager.load_session.return_value = mock_session
    ctx.auth_manager.get_token_from_env.return_value = None

    with patch("xnatctl.cli.common.XNATClient") as mock_client_cls:
        ctx.get_client()

    assert mock_client_cls.call_args.kwargs["username"] == "Ricky_Wong"
    assert mock_client_cls.call_args.kwargs["session_token"] == "cached-token"


def test_get_client_enables_auto_reauth():
    """The client re-authenticates on a mid-command 401 (issue #20)."""
    ctx = Context()
    ctx.config = Config(
        default_profile="default",
        profiles={"default": Profile(url="https://example.org", verify_ssl=False)},
    )
    ctx.auth_manager = MagicMock()
    ctx.auth_manager.load_session.return_value = None
    ctx.auth_manager.get_token_from_env.return_value = None

    with patch("xnatctl.cli.common.XNATClient") as mock_client_cls:
        ctx.get_client()

    assert mock_client_cls.call_args.kwargs["auto_reauth"] is True


# =============================================================================
# apply_filter / apply_sort_limit / resolve_columns
# =============================================================================

_ROWS = [
    {"id": "A", "label": "SUB001"},
    {"id": "B", "label": "CTL001"},
    {"id": "C", "label": "SUB002"},
]


class TestApplyFilter:
    """Tests for the shared --filter helper."""

    def test_no_expr_returns_rows_unchanged(self) -> None:
        assert apply_filter(_ROWS, None) is _ROWS

    def test_glob_matches_by_field(self) -> None:
        result = apply_filter(_ROWS, "label:SUB*")
        assert [r["id"] for r in result] == ["A", "C"]

    def test_no_match_yields_empty_list(self) -> None:
        assert apply_filter(_ROWS, "label:ZZZ*") == []

    def test_case_insensitive(self) -> None:
        result = apply_filter(_ROWS, "label:sub*")
        assert [r["id"] for r in result] == ["A", "C"]

    def test_missing_colon_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match="field:glob"):
            apply_filter(_ROWS, "labelSUB")

    def test_empty_field_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match="field:glob"):
            apply_filter(_ROWS, ":SUB*")

    def test_unknown_field_raises_usage_error(self) -> None:
        """A typo'd field must fail loudly, not silently match every row
        by comparing against an empty string.
        """
        with pytest.raises(click.UsageError, match="Unknown filter field 'lable'.*id.*label"):
            apply_filter(_ROWS, "lable:SUB*")

    def test_unknown_field_against_empty_rows_does_not_raise(self) -> None:
        """No rows means no known field set to validate against -- filtering
        nothing trivially yields nothing rather than erroring.
        """
        assert apply_filter([], "missing:*") == []

    def test_field_present_on_some_but_not_all_rows_is_known(self) -> None:
        """The available-field set is the union across every row, not just
        the first (rows are not required to be uniform).
        """
        rows = [{"id": "A", "extra": "x"}, {"id": "B"}]
        result = apply_filter(rows, "extra:x")
        assert [r["id"] for r in result] == ["A"]


class TestApplySortLimit:
    """Tests for the shared --sort-by/--limit helper."""

    def test_no_sort_no_limit_returns_rows_unchanged(self) -> None:
        assert apply_sort_limit(_ROWS, None, None) == _ROWS

    def test_sort_by_field_ascending(self) -> None:
        result = apply_sort_limit(_ROWS, "label", None)
        assert [r["id"] for r in result] == ["B", "A", "C"]

    def test_sort_by_field_desc(self) -> None:
        result = apply_sort_limit(_ROWS, "label:desc", None)
        assert [r["id"] for r in result] == ["C", "A", "B"]

    def test_sort_by_field_explicit_asc(self) -> None:
        result = apply_sort_limit(_ROWS, "label:asc", None)
        assert [r["id"] for r in result] == ["B", "A", "C"]

    def test_invalid_direction_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match="asc.*desc"):
            apply_sort_limit(_ROWS, "label:sideways", None)

    def test_limit_truncates(self) -> None:
        result = apply_sort_limit(_ROWS, None, 2)
        assert len(result) == 2

    def test_limit_zero_yields_no_rows(self) -> None:
        assert apply_sort_limit(_ROWS, None, 0) == []

    def test_limit_none_keeps_everything(self) -> None:
        assert apply_sort_limit(_ROWS, None, None) == _ROWS

    def test_sort_then_limit_combined(self) -> None:
        result = apply_sort_limit(_ROWS, "label:desc", 1)
        assert [r["id"] for r in result] == ["C"]

    def test_missing_field_sorts_last_ascending(self) -> None:
        rows = [{"id": "A", "n": 2}, {"id": "B"}, {"id": "C", "n": 1}]
        result = apply_sort_limit(rows, "n", None)
        assert [r["id"] for r in result] == ["C", "A", "B"]

    def test_missing_field_sorts_last_descending(self) -> None:
        """Missing values stay at the bottom under :desc too, not flip to the top."""
        rows = [{"id": "A", "n": 2}, {"id": "B"}, {"id": "C", "n": 1}]
        result = apply_sort_limit(rows, "n:desc", None)
        assert [r["id"] for r in result] == ["A", "C", "B"]

    def test_numeric_strings_sort_numerically_not_lexicographically(self) -> None:
        rows = [{"id": "A", "n": "10"}, {"id": "B", "n": "2"}, {"id": "C", "n": "1"}]
        result = apply_sort_limit(rows, "n", None)
        assert [r["id"] for r in result] == ["C", "B", "A"]

    def test_numeric_ints_sort_numerically_descending(self) -> None:
        rows = [{"id": "A", "n": 10}, {"id": "B", "n": 2}, {"id": "C", "n": 1}]
        result = apply_sort_limit(rows, "n:desc", None)
        assert [r["id"] for r in result] == ["A", "B", "C"]

    def test_non_numeric_column_falls_back_to_string_sort(self) -> None:
        """A column that isn't uniformly numeric-coercible sorts as text."""
        rows = [{"id": "A", "n": "10"}, {"id": "B", "n": "abc"}, {"id": "C", "n": "2"}]
        result = apply_sort_limit(rows, "n", None)
        assert [r["id"] for r in result] == ["A", "C", "B"]


class TestResolveColumns:
    """Tests for the shared --columns validator."""

    def test_none_returns_default_columns(self) -> None:
        assert resolve_columns(["id", "label"], None) == ["id", "label"]

    def test_requested_columns_in_order(self) -> None:
        assert resolve_columns(["id", "label", "date"], "date,id") == ["date", "id"]

    def test_unknown_column_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match="bogus"):
            resolve_columns(["id", "label"], "id,bogus")

    def test_comma_only_raises_usage_error(self) -> None:
        """`--columns ","` must not silently resolve to an empty list --
        print_output treats an empty `columns` as falsy and falls through
        to JSON regardless of the requested table/quiet format.
        """
        with pytest.raises(click.UsageError, match="no column names found"):
            resolve_columns(["id", "label"], ",")

    def test_explicit_empty_string_raises_usage_error(self) -> None:
        """`--columns ''` is a caller mistake, not "use the defaults" --
        only omitting the flag entirely (None) means that.
        """
        with pytest.raises(click.UsageError, match="no column names found"):
            resolve_columns(["id", "label"], "")

    def test_whitespace_only_raises_usage_error(self) -> None:
        with pytest.raises(click.UsageError, match="no column names found"):
            resolve_columns(["id", "label"], "   ")


class TestParseBatchText:
    """Tests for the _parse_batch_text parsing helper."""

    def test_empty_text_yields_empty_list(self) -> None:
        assert _parse_batch_text("") == []
        assert _parse_batch_text("   \n  \n") == []

    def test_one_per_line_skips_blank_lines(self) -> None:
        assert _parse_batch_text("A\n\nB\n  \nC\n") == ["A", "B", "C"]

    def test_one_per_line_strips_whitespace(self) -> None:
        assert _parse_batch_text("  A  \n B \n") == ["A", "B"]

    def test_json_array_of_strings(self) -> None:
        assert _parse_batch_text('["A", "B", "C"]') == ["A", "B", "C"]

    def test_json_array_skips_blank_entries(self) -> None:
        assert _parse_batch_text('["A", "", "  ", "B"]') == ["A", "B"]

    def test_json_array_with_non_string_raises(self) -> None:
        with pytest.raises(click.UsageError, match="only strings"):
            _parse_batch_text("[1, 2, 3]")

    def test_json_object_raises(self) -> None:
        with pytest.raises(click.UsageError, match="array of strings"):
            _parse_batch_text('{"a": 1}')

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(click.UsageError, match="invalid JSON"):
            _parse_batch_text("[1, 2")


@click.command()
@click.option("--yes", "-y", is_flag=True)
@click.option("--dry-run", is_flag=True)
@batch_option
def _batch_probe(yes: bool, dry_run: bool, batch_ids: list[str] | None) -> None:
    """A minimal command carrying only @batch_option, for testing it in isolation."""
    click.echo(repr(batch_ids))


class TestBatchOption:
    """Tests for the @batch_option decorator."""

    def test_no_batch_flag_injects_none(self) -> None:
        result = CliRunner().invoke(_batch_probe, [])
        assert result.exit_code == 0
        assert result.output.strip() == "None"

    def test_reads_one_per_line_file(self, tmp_path: Any) -> None:
        path = tmp_path / "ids.txt"
        path.write_text("SUB001\nSUB002\n\nSUB003\n")
        result = CliRunner().invoke(_batch_probe, ["--batch", str(path)])
        assert result.exit_code == 0
        assert result.output.strip() == "['SUB001', 'SUB002', 'SUB003']"

    def test_reads_json_array_file(self, tmp_path: Any) -> None:
        path = tmp_path / "ids.json"
        path.write_text('["SUB001", "SUB002"]')
        result = CliRunner().invoke(_batch_probe, ["--batch", str(path)])
        assert result.exit_code == 0
        assert result.output.strip() == "['SUB001', 'SUB002']"

    def test_missing_file_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(_batch_probe, ["--batch", "/no/such/file"])
        assert result.exit_code != 0

    def test_dash_reads_stdin(self) -> None:
        result = CliRunner().invoke(
            _batch_probe, ["--batch", "-", "--yes"], input="SUB001\nSUB002\n"
        )
        assert result.exit_code == 0
        assert result.output.strip() == "['SUB001', 'SUB002']"

    def test_dash_with_dry_run_instead_of_yes(self) -> None:
        result = CliRunner().invoke(_batch_probe, ["--batch", "-", "--dry-run"], input="SUB001\n")
        assert result.exit_code == 0
        assert result.output.strip() == "['SUB001']"

    def test_dash_without_yes_or_dry_run_is_usage_error(self) -> None:
        result = CliRunner().invoke(_batch_probe, ["--batch", "-"], input="SUB001\n")
        assert result.exit_code != 0
        assert "requires --yes or --dry-run" in result.output

    def test_dash_rejects_interactive_terminal(self) -> None:
        """--batch - must refuse to read on a tty rather than block forever
        waiting for an EOF that never comes, even with --yes given.

        CliRunner's captured stdin always reports isatty()=False, so this
        goes around it and calls .main() directly with a mocked sys.stdin,
        rather than through CliRunner.invoke().
        """
        with patch("xnatctl.cli.common.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with pytest.raises(click.UsageError, match="interactive terminal"):
                _batch_probe.main(["--batch", "-", "--yes"], standalone_mode=False)
            mock_stdin.read.assert_not_called()

    def test_empty_batch_text_is_a_usage_error(self, tmp_path: Any) -> None:
        """A --batch file that is present but parses to no IDs must be its
        own clear error, not silently read as "--batch was not given".
        """
        path = tmp_path / "empty.txt"
        path.write_text("")
        result = CliRunner().invoke(_batch_probe, ["--batch", str(path)])
        assert result.exit_code != 0
        assert "--batch produced no IDs" in result.output

    def test_whitespace_only_batch_text_is_a_usage_error(self, tmp_path: Any) -> None:
        path = tmp_path / "blank.txt"
        path.write_text("   \n  \n")
        result = CliRunner().invoke(_batch_probe, ["--batch", str(path)])
        assert result.exit_code != 0
        assert "--batch produced no IDs" in result.output

    def test_dash_empty_stdin_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(_batch_probe, ["--batch", "-", "--yes"], input="")
        assert result.exit_code != 0
        assert "--batch produced no IDs" in result.output
