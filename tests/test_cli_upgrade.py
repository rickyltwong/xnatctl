"""Tests for `xnatctl upgrade`: install-method detection, dry-run vs. --yes,
--check, and the frozen-binary self-replace.

Detection is exercised through :func:`detect_upgrade_method`'s injectable
seams (``env``/``frozen``/``install_path``/``dockerenv_path``) directly --
never the real ``sys.frozen``, ``PIPX_HOME``, or ``/.dockerenv``. The CLI
layer is exercised through ``CliRunner``, with ``detect_upgrade_method`` and
``subprocess.run`` monkeypatched so no test ever executes a real package
manager or touches the network. ``replace_frozen_binary``'s own download
seam is injected with local files built by :func:`_build_release_archive`,
so the frozen self-replace tests never touch the network either.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import config_seam

from xnatctl import __version__
from xnatctl.cli import main as cli_main
from xnatctl.cli import upgrade as upgrade_mod
from xnatctl.cli.common import ExitCode
from xnatctl.cli.main import cli
from xnatctl.core import update_check
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import UpgradeError


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test off the developer's real ``~/.config/xnatctl/.update-check``."""
    path = tmp_path / ".update-check"
    monkeypatch.setattr(update_check, "UPDATE_CACHE_FILE", path)
    return path


def _seed_cache(path: Path, *, latest: str) -> None:
    path.write_text(json.dumps({"last_check": time.time(), "latest": latest}))


# =============================================================================
# detect_upgrade_method: one test per branch, all via injected seams
# =============================================================================


def test_detect_frozen() -> None:
    method = upgrade_mod.detect_upgrade_method(frozen=True)
    assert method.kind == "frozen"
    assert method.command is None


def test_detect_pipx_via_pipx_home(tmp_path: Path) -> None:
    pipx_home = tmp_path / "pipx"
    install_path = (
        pipx_home
        / "venvs"
        / "xnatctl"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "xnatctl"
        / "__init__.py"
    )
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")

    method = upgrade_mod.detect_upgrade_method(
        env={"PIPX_HOME": str(pipx_home)},
        frozen=False,
        install_path=install_path,
        dockerenv_path=tmp_path / "no-dockerenv",
        home=tmp_path / "unrelated-home",
    )

    assert method.kind == "pipx"
    assert method.command == ("pipx", "upgrade", "xnatctl")


def test_detect_pipx_legacy_default_dir(tmp_path: Path) -> None:
    """Pipx's pre-1.0 default root, with no PIPX_HOME set."""
    home = tmp_path / "home"
    install_path = (
        home
        / ".local"
        / "pipx"
        / "venvs"
        / "xnatctl"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "xnatctl"
        / "__init__.py"
    )
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")

    method = upgrade_mod.detect_upgrade_method(
        env={}, frozen=False, install_path=install_path, dockerenv_path=tmp_path / "nope", home=home
    )

    assert method.kind == "pipx"


def test_detect_pipx_modern_xdg_data_dir(tmp_path: Path) -> None:
    """Modern pipx (>=1.0) defaults to the XDG data dir, not ~/.local/pipx."""
    home = tmp_path / "home"
    install_path = (
        home
        / ".local"
        / "share"
        / "pipx"
        / "venvs"
        / "xnatctl"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "xnatctl"
        / "__init__.py"
    )
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")

    method = upgrade_mod.detect_upgrade_method(
        env={}, frozen=False, install_path=install_path, dockerenv_path=tmp_path / "nope", home=home
    )

    assert method.kind == "pipx"


def test_detect_pipx_generic_marker_fallback(tmp_path: Path) -> None:
    """A relocated/distro-patched pipx whose root matches neither known default."""
    install_path = (
        tmp_path
        / "opt"
        / "custom"
        / "pipx"
        / "venvs"
        / "xnatctl"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "xnatctl"
        / "__init__.py"
    )
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")

    method = upgrade_mod.detect_upgrade_method(
        env={},
        frozen=False,
        install_path=install_path,
        dockerenv_path=tmp_path / "nope",
        home=tmp_path / "unrelated-home",
    )

    assert method.kind == "pipx"


def test_detect_docker_takes_precedence_over_pip(tmp_path: Path) -> None:
    """The published image installs via plain `pip install .` (see the repo
    Dockerfile) -- without Docker being checked before the pip/uv fallback,
    every containerized install would be misreported as pip-managed.
    """
    install_path = (
        tmp_path / "venv" / "lib" / "python3.11" / "site-packages" / "xnatctl" / "__init__.py"
    )
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")
    dockerenv = tmp_path / ".dockerenv"
    dockerenv.write_text("")

    method = upgrade_mod.detect_upgrade_method(
        env={},
        frozen=False,
        install_path=install_path,
        dockerenv_path=dockerenv,
        home=tmp_path / "unrelated-home",
    )

    assert method.kind == "docker"
    assert method.command == ("docker", "pull", "ghcr.io/rickyltwong/xnatctl:latest")


def test_detect_uv_managed_venv(tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    install_path = site_packages / "xnatctl" / "__init__.py"
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("uv = 0.4.0\n")

    method = upgrade_mod.detect_upgrade_method(
        env={},
        frozen=False,
        install_path=install_path,
        dockerenv_path=tmp_path / "no-dockerenv",
        home=tmp_path / "unrelated-home",
    )

    assert method.kind == "uv"
    assert method.command == ("uv", "pip", "install", "-U", "xnatctl")


def test_detect_pip_fallback(tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    install_path = site_packages / "xnatctl" / "__init__.py"
    install_path.parent.mkdir(parents=True)
    install_path.write_text("")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")

    method = upgrade_mod.detect_upgrade_method(
        env={},
        frozen=False,
        install_path=install_path,
        dockerenv_path=tmp_path / "no-dockerenv",
        home=tmp_path / "unrelated-home",
    )

    assert method.kind == "pip"
    assert method.command is not None
    assert method.command[0] == sys.executable


# =============================================================================
# CLI: dry-run vs. --yes, per detected method (detection itself monkeypatched)
# =============================================================================


def _pip_method() -> upgrade_mod.UpgradeMethod:
    return upgrade_mod.UpgradeMethod(
        "pip", "pip-managed environment", ("pip", "install", "-U", "xnatctl")
    )


def test_dry_run_prints_and_does_not_execute(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upgrade_mod, "detect_upgrade_method", _pip_method)
    calls: list[Any] = []
    monkeypatch.setattr(upgrade_mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    result = runner.invoke(cli, ["upgrade"])

    assert result.exit_code == 0
    assert calls == []
    assert "pip-managed environment" in result.stdout
    assert "pip install -U xnatctl" in result.stdout
    assert "--yes" in result.stderr


def test_yes_executes_expected_argv(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_mod, "detect_upgrade_method", _pip_method)
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0

    def fake_run(argv: list[str], check: bool = False) -> _Completed:
        calls.append(argv)
        return _Completed()

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["upgrade", "--yes"])

    assert result.exit_code == 0
    assert calls == [["pip", "install", "-U", "xnatctl"]]


def test_yes_propagates_nonzero_exit(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_mod, "detect_upgrade_method", _pip_method)

    class _Completed:
        returncode = 7

    monkeypatch.setattr(upgrade_mod.subprocess, "run", lambda *a, **k: _Completed())

    result = runner.invoke(cli, ["upgrade", "--yes"])

    assert result.exit_code == 7


def test_docker_never_subprocesses(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    method = upgrade_mod.UpgradeMethod(
        "docker", "Docker container", ("docker", "pull", "ghcr.io/rickyltwong/xnatctl:latest")
    )
    monkeypatch.setattr(upgrade_mod, "detect_upgrade_method", lambda: method)
    calls: list[Any] = []
    monkeypatch.setattr(upgrade_mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    result_dry = runner.invoke(cli, ["upgrade"])
    result_yes = runner.invoke(cli, ["upgrade", "--yes"])

    assert result_dry.exit_code == 0
    assert result_yes.exit_code == 0
    assert calls == []
    assert "docker pull ghcr.io/rickyltwong/xnatctl:latest" in result_dry.stdout
    assert "docker pull ghcr.io/rickyltwong/xnatctl:latest" in result_yes.stdout


def test_frozen_dry_run_prints_and_does_not_fetch(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "xnatctl"
    target.write_bytes(b"binary")
    monkeypatch.setattr(
        upgrade_mod,
        "detect_upgrade_method",
        lambda: upgrade_mod.UpgradeMethod("frozen", "standalone binary (PyInstaller)", None),
    )
    monkeypatch.setattr(upgrade_mod.sys, "executable", str(target))

    def boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError("must not fetch/replace during a dry run")

    monkeypatch.setattr(upgrade_mod, "replace_frozen_binary", boom)

    result = runner.invoke(cli, ["upgrade"])

    assert result.exit_code == 0
    assert "standalone binary (PyInstaller)" in result.stdout
    assert "--yes" in result.stderr


def test_frozen_unwritable_target_prints_manual_instructions_and_exits_nonzero(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "xnatctl"
    target.write_bytes(b"binary")
    monkeypatch.setattr(
        upgrade_mod,
        "detect_upgrade_method",
        lambda: upgrade_mod.UpgradeMethod("frozen", "standalone binary (PyInstaller)", None),
    )
    monkeypatch.setattr(upgrade_mod.sys, "executable", str(target))
    monkeypatch.setattr(upgrade_mod.os, "access", lambda path, mode: False)
    called: list[Any] = []
    monkeypatch.setattr(upgrade_mod, "replace_frozen_binary", lambda *a, **k: called.append(1))

    result = runner.invoke(cli, ["upgrade", "--yes"])

    assert result.exit_code == ExitCode.PERMISSION_ERROR
    assert called == []
    # Collapse whitespace before matching: print_error renders through Rich,
    # which word-wraps to the console width -- "not writable" can straddle a
    # line break when target.parent is a long path. The message's presence
    # is the contract; where the wrap lands is not.
    assert "not writable" in " ".join(result.stderr.split())


# =============================================================================
# --check: up-to-date / newer-available / unreachable
# =============================================================================


def test_check_up_to_date(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda timeout=10.0: __version__)

    result = runner.invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == 0
    # print_success also renders through Rich's word-wrap -- see the same
    # normalization in test_frozen_unwritable_target_...exits_nonzero above.
    assert "up to date" in " ".join(result.stderr.split())


def test_check_newer_available(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda timeout=10.0: "999.0.0")

    result = runner.invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == 0
    assert "999.0.0" in result.stdout
    assert "xnatctl upgrade" in result.stdout


def test_check_unreachable(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda timeout=10.0: None)

    result = runner.invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == ExitCode.NETWORK_ERROR
    # print_error also renders through Rich's word-wrap -- see the same
    # normalization in test_frozen_unwritable_target_...exits_nonzero above.
    assert "Could not reach PyPI" in " ".join(result.stderr.split())


def test_check_updates_the_cache(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolate_cache: Path
) -> None:
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda timeout=10.0: "999.0.0")

    runner.invoke(cli, ["upgrade", "--check"])

    data = json.loads(isolate_cache.read_text())
    assert data["latest"] == "999.0.0"


def test_check_never_subprocesses(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda timeout=10.0: "999.0.0")
    calls: list[Any] = []
    monkeypatch.setattr(upgrade_mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    runner.invoke(cli, ["upgrade", "--check"])

    assert calls == []


# =============================================================================
# Frozen self-replace: real download/verify/extract/replace, network seamed
# =============================================================================


def _build_release_archive(
    dest_dir: Path, os_name: str, arch: str, *, content: bytes = b"new-binary-bytes"
) -> tuple[Path, Path]:
    """Build a fake release archive + sha256 sidecar matching the real CI shape."""
    binary_name = "xnatctl.exe" if os_name == "windows" else "xnatctl"
    asset_name = upgrade_mod._release_asset_name(os_name, arch)
    archive_path = dest_dir / asset_name

    if os_name == "windows":
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr(binary_name, content)
    else:
        with tarfile.open(archive_path, "w:gz") as tf:
            info = tarfile.TarInfo(name=binary_name)
            info.size = len(content)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(content))

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = dest_dir / f"{asset_name}.sha256"
    checksum_path.write_text(f"{digest}  {asset_name}\n")
    return archive_path, checksum_path


def _fake_download(src_archive: Path, src_checksum: Path) -> Any:
    def download(url: str, dest: Path) -> None:
        shutil.copy2(src_checksum if url.endswith(".sha256") else src_archive, dest)

    return download


def _noop_verify(path: Path) -> None:
    del path


def test_replace_frozen_binary_downloads_verifies_and_installs(tmp_path: Path) -> None:
    target_dir = tmp_path / "install"
    target_dir.mkdir()
    target = target_dir / "xnatctl"
    target.write_bytes(b"old-binary-bytes")

    os_name = upgrade_mod._detect_os()
    arch = upgrade_mod._detect_arch()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    src_archive, src_checksum = _build_release_archive(release_dir, os_name, arch)

    tag = upgrade_mod.replace_frozen_binary(
        target,
        fetch_release_tag=lambda: "v9.9.9",
        download=_fake_download(src_archive, src_checksum),
        verify=_noop_verify,
    )

    assert tag == "v9.9.9"
    assert target.read_bytes() == b"new-binary-bytes"
    backup = target.with_name(target.name + ".bak")
    if os_name == "windows":
        # Windows can't delete the pre-upgrade binary while this process is
        # still running under it -- replace_frozen_binary renames it aside
        # instead and leaves it there; cleanup_stale_backup removes it on a
        # later launch (see test_replace_frozen_binary_windows_renames_running_exe_aside).
        assert backup.exists()
    else:
        assert not backup.exists()
        assert os.access(target, os.X_OK)


def test_replace_frozen_binary_checksum_mismatch_leaves_target_untouched(
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / "install"
    target_dir.mkdir()
    target = target_dir / "xnatctl"
    target.write_bytes(b"old-binary-bytes")

    os_name = upgrade_mod._detect_os()
    arch = upgrade_mod._detect_arch()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    src_archive, src_checksum = _build_release_archive(release_dir, os_name, arch)
    asset_name = upgrade_mod._release_asset_name(os_name, arch)
    src_checksum.write_text(f"{'0' * 64}  {asset_name}\n")  # deliberately wrong

    with pytest.raises(UpgradeError, match="Checksum mismatch"):
        upgrade_mod.replace_frozen_binary(
            target,
            fetch_release_tag=lambda: "v1.2.3",
            download=_fake_download(src_archive, src_checksum),
            verify=_noop_verify,
        )

    assert target.read_bytes() == b"old-binary-bytes"


def test_replace_frozen_binary_no_release_tag_raises(tmp_path: Path) -> None:
    target = tmp_path / "xnatctl"
    target.write_bytes(b"old-binary-bytes")

    with pytest.raises(UpgradeError, match="latest release"):
        upgrade_mod.replace_frozen_binary(
            target,
            fetch_release_tag=lambda: None,
            download=lambda url, dest: None,
            verify=_noop_verify,
        )


def test_replace_frozen_binary_candidate_verify_fails_leaves_target_untouched(
    tmp_path: Path,
) -> None:
    """A valid checksum doesn't prove the binary RUNS -- the pre-install
    --version sanity check must catch a candidate that downloads and
    verifies fine but can't actually execute, before target is touched.
    """
    target_dir = tmp_path / "install"
    target_dir.mkdir()
    target = target_dir / "xnatctl"
    target.write_bytes(b"old-binary-bytes")

    os_name = upgrade_mod._detect_os()
    arch = upgrade_mod._detect_arch()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    src_archive, src_checksum = _build_release_archive(release_dir, os_name, arch)

    def failing_verify(path: Path) -> None:
        raise UpgradeError(f"{path} is not a valid xnatctl binary")

    with pytest.raises(UpgradeError, match="not a valid xnatctl binary"):
        upgrade_mod.replace_frozen_binary(
            target,
            fetch_release_tag=lambda: "v1.0.0",
            download=_fake_download(src_archive, src_checksum),
            verify=failing_verify,
        )

    assert target.read_bytes() == b"old-binary-bytes"
    assert not target.with_name(target.name + ".bak").exists()


def test_replace_frozen_binary_post_install_verify_fails_restores_backup(
    tmp_path: Path,
) -> None:
    """The candidate passes its pre-install check but the INSTALLED copy
    fails its own -- e.g. a filesystem quirk corrupted it mid-copy. Must
    restore automatically rather than leave a broken binary in place.
    """
    target_dir = tmp_path / "install"
    target_dir.mkdir()
    target = target_dir / "xnatctl"
    target.write_bytes(b"old-binary-bytes")

    os_name = upgrade_mod._detect_os()
    arch = upgrade_mod._detect_arch()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    src_archive, src_checksum = _build_release_archive(release_dir, os_name, arch)

    calls: list[Path] = []

    def verify_fails_on_second_call(path: Path) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise UpgradeError("simulated: installed binary won't run")

    with pytest.raises(UpgradeError, match="restored the previous version"):
        upgrade_mod.replace_frozen_binary(
            target,
            fetch_release_tag=lambda: "v1.0.0",
            download=_fake_download(src_archive, src_checksum),
            verify=verify_fails_on_second_call,
        )

    assert len(calls) == 2
    assert target.read_bytes() == b"old-binary-bytes"  # rolled back
    assert not target.with_name(target.name + ".bak").exists()  # POSIX: cleaned up


def test_replace_frozen_binary_windows_renames_running_exe_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows can't os.replace() over a running .exe -- the OS locks the
    mapped image against overwrite/delete. The pre-upgrade binary must be
    renamed aside instead (legal while running) and left for a later launch
    to clean up, never deleted here.
    """
    monkeypatch.setattr(upgrade_mod, "_detect_os", lambda: "windows")
    target_dir = tmp_path / "install"
    target_dir.mkdir()
    target = target_dir / "xnatctl.exe"
    target.write_bytes(b"old-binary-bytes")

    arch = upgrade_mod._detect_arch()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    src_archive, src_checksum = _build_release_archive(release_dir, "windows", arch)

    tag = upgrade_mod.replace_frozen_binary(
        target,
        fetch_release_tag=lambda: "v2.0.0",
        download=_fake_download(src_archive, src_checksum),
        verify=_noop_verify,
    )

    assert tag == "v2.0.0"
    assert target.read_bytes() == b"new-binary-bytes"
    backup = target.with_name(target.name + ".bak")
    assert backup.exists()  # left for cleanup_stale_backup() on the next launch
    assert backup.read_bytes() == b"old-binary-bytes"


def test_replace_frozen_binary_windows_post_install_verify_fails_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upgrade_mod, "_detect_os", lambda: "windows")
    target_dir = tmp_path / "install"
    target_dir.mkdir()
    target = target_dir / "xnatctl.exe"
    target.write_bytes(b"old-binary-bytes")

    arch = upgrade_mod._detect_arch()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    src_archive, src_checksum = _build_release_archive(release_dir, "windows", arch)

    calls: list[Path] = []

    def verify_fails_on_second_call(path: Path) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise UpgradeError("simulated: installed binary won't run")

    with pytest.raises(UpgradeError, match="restored the previous version"):
        upgrade_mod.replace_frozen_binary(
            target,
            fetch_release_tag=lambda: "v2.0.0",
            download=_fake_download(src_archive, src_checksum),
            verify=verify_fails_on_second_call,
        )

    assert target.read_bytes() == b"old-binary-bytes"
    assert not target.with_name(target.name + ".bak").exists()


# =============================================================================
# cleanup_stale_backup: startup hygiene for a leftover .bak
# =============================================================================


def test_cleanup_stale_backup_removes_leftover_bak(tmp_path: Path) -> None:
    target = tmp_path / "xnatctl"
    backup = tmp_path / "xnatctl.bak"
    backup.write_bytes(b"stale")

    upgrade_mod.cleanup_stale_backup(target)

    assert not backup.exists()


def test_cleanup_stale_backup_missing_is_silent(tmp_path: Path) -> None:
    target = tmp_path / "xnatctl"
    upgrade_mod.cleanup_stale_backup(target)  # must not raise


def test_frozen_cli_invocation_cleans_stale_backup_at_startup(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(cli_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli_main, "cleanup_stale_backup", lambda: calls.append(1))

    result = runner.invoke(cli, ["completion", "bash"])

    assert result.exit_code == 0
    assert calls == [1]


def test_non_frozen_cli_invocation_never_calls_cleanup(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(cli_main, "cleanup_stale_backup", lambda: calls.append(1))

    result = runner.invoke(cli, ["completion", "bash"])

    assert result.exit_code == 0
    assert calls == []


# =============================================================================
# Config opt-out: update_check: false suppresses the notice
# =============================================================================


@pytest.fixture
def notify_path_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "_running_under_pytest", lambda: False)
    for var in ("NO_UPDATE_NOTIFIER", "XNAT_NO_UPDATE_CHECK", "CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(update_check, "refresh_cache_async", lambda **kwargs: None)


def _config(*, update_check_enabled: bool) -> Config:
    return Config(
        default_profile="default",
        update_check=update_check_enabled,
        profiles={"default": Profile(url="https://xnat.example.org", name="default")},
    )


def test_notice_suppressed_when_config_update_check_false(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    notify_path_reachable: None,
    isolate_cache: Path,
) -> None:
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)
    _seed_cache(isolate_cache, latest="999.0.0")
    monkeypatch.setattr(upgrade_mod, "detect_upgrade_method", _pip_method)

    with config_seam(_config(update_check_enabled=False)):
        result = runner.invoke(cli, ["upgrade"])

    assert result.exit_code == 0
    assert "xnatctl release is available" not in result.stderr


def test_notice_shown_when_config_update_check_true(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    notify_path_reachable: None,
    isolate_cache: Path,
) -> None:
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)
    _seed_cache(isolate_cache, latest="999.0.0")
    monkeypatch.setattr(upgrade_mod, "detect_upgrade_method", _pip_method)

    with config_seam(_config(update_check_enabled=True)):
        result = runner.invoke(cli, ["upgrade"])

    assert result.exit_code == 0
    assert "xnatctl release is available" in result.stderr
    assert "xnatctl upgrade" in result.stderr
