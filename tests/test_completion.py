from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli

BASH = shutil.which("bash")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _completion(runner: CliRunner, shell: str) -> str:
    result = runner.invoke(cli, ["completion", shell])
    assert result.exit_code == 0, result.output
    return result.output


def test_bash_uses_click8_type_value_protocol(runner) -> None:
    out = _completion(runner, "bash")
    assert "IFS=','" in out
    assert "_xnatctl_completion" in out
    assert "COMPREPLY=( $( env COMP_WORDS" not in out


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_bash_completion_parses_plain_values(tmp_path, runner) -> None:
    script = tmp_path / "script.sh"
    script.write_text(_completion(runner, "bash"))

    fake = tmp_path / "xnatctl"
    fake.write_text('#!/usr/bin/env bash\nprintf "plain,project\\nplain,prearchive\\n"\n')
    fake.chmod(0o755)

    driver = tmp_path / "driver.sh"
    driver.write_text(
        textwrap.dedent(
            f"""
            source "{script}"
            COMP_WORDS=(xnatctl pr)
            COMP_CWORD=1
            _xnatctl_completion xnatctl
            printf '%s\\n' "${{COMPREPLY[@]}}"
            """
        )
    )

    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    result = subprocess.run(
        [BASH, str(driver)], capture_output=True, text=True, env=env, timeout=30
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert "project" in lines
    assert "prearchive" in lines
    assert "plain,project" not in lines


def test_zsh_matches_installed_click_template(runner) -> None:
    from click.shell_completion import get_completion_class

    out = _completion(runner, "zsh").strip()
    expected = get_completion_class("zsh")(cli, {}, "xnatctl", "_XNATCTL_COMPLETE").source().strip()
    assert out == expected
    assert "#compdef xnatctl" in out


def test_fish_output_uses_type_value_parsing(runner) -> None:
    out = _completion(runner, "fish")
    assert "xnatctl" in out
    assert "function" in out
    assert "string split" in out
