"""``xnatctl upgrade``: detect how this install was made, and update it in place.

Detection order: frozen PyInstaller binary, then pipx, then Docker, then a
plain pip/uv virtual environment as the fallback. Docker is checked before
the pip/uv fallback (not after, as the bullet order above might suggest) --
the published image installs xnatctl with a plain ``pip install .`` (see the
repo ``Dockerfile``), so without that ordering every containerized install
would be misreported as "pip-managed" and offered a ``pip install -U`` that
cannot work: the running container's filesystem changes are thrown away the
moment it exits, and a locked-down container may not even have write access
to its own site-packages.

Dry by default: the command only ever detects and prints. ``--yes/-y`` is
required to actually run anything. The frozen-binary path never subprocesses
a package manager -- there is none to shell out to -- it downloads the
matching release asset from GitHub, verifies it against the release's
``.sha256`` sidecar (see ``install.sh``, which this mirrors), runs the
downloaded candidate's own ``--version`` as a sanity check (a checksum only
proves the bytes weren't corrupted in transit, not that the binary actually
runs -- wrong arch/libc, a bad extraction, ...), and only then atomically
installs it over ``sys.executable``: writing the new binary to a sibling
temp file on the same filesystem first (so the install is a real atomic
rename, never a partial write observable by a concurrent invocation), then
running ``--version`` again against the INSTALLED binary and automatically
rolling back to a kept ``.bak`` copy if that second check fails. On Windows,
where the OS locks a running ``.exe`` against overwrite or delete, the
pre-upgrade binary is renamed aside (legal while running) rather than copied
and replaced, and the ``.bak`` it leaves behind can only be removed once
this process exits -- :func:`cleanup_stale_backup`, invoked at the top of
every frozen invocation, does that on the next launch. Docker is never
subprocessed regardless of ``--yes``: this process cannot replace the image
it is running in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import click
from packaging.version import InvalidVersion, Version

import xnatctl
from xnatctl.cli.common import Context, ExitCode, global_options, handle_errors
from xnatctl.core import update_check
from xnatctl.core.exceptions import UpgradeError
from xnatctl.core.output import print_error, print_hint, print_success

logger = logging.getLogger(__name__)

GITHUB_REPO = "rickyltwong/xnatctl"
GITHUB_RELEASE_DOWNLOAD_BASE = f"https://github.com/{GITHUB_REPO}/releases/download"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GHCR_IMAGE = f"ghcr.io/{GITHUB_REPO}:latest"

#: Longer than the passive per-command cache refresh's 1s ceiling -- this is
#: an explicit, user-requested check, so waiting a bit longer for a real
#: answer is the right tradeoff (see ``update_check.fetch_latest_version``).
CHECK_FETCH_TIMEOUT_SECONDS = 10.0
DOWNLOAD_TIMEOUT_SECONDS = 30.0

#: Seam so tests never touch the real filesystem root.
DOCKERENV_PATH = Path("/.dockerenv")


# =============================================================================
# Install-method detection
# =============================================================================


@dataclass(frozen=True)
class UpgradeMethod:
    """One detected way to upgrade this install."""

    kind: str
    """``"frozen" | "pipx" | "uv" | "pip" | "docker"``."""

    description: str
    """Human-readable label for the detected install, e.g. "pipx-managed install"."""

    command: tuple[str, ...] | None
    """Argv to run for ``--yes``. ``None`` for ``frozen`` (no shell-out; see
    :func:`replace_frozen_binary`) and unused for ``docker`` (printed, never run)."""

    def render_command(self) -> str:
        """Human-readable form of :attr:`command`, for the dry-run/echo line."""
        command = self.command
        assert command is not None
        return " ".join(command)


def _pipx_candidate_venv_dirs(env: Mapping[str, str], home: Path) -> list[Path]:
    """Every plausible pipx venvs root, in priority order.

    ``PIPX_HOME`` (if set) always wins -- it's the explicit override pipx
    itself honors. Otherwise both known defaults are checked: pipx used
    ``~/.local/pipx`` through around 1.0, then moved to the XDG data dir
    (``~/.local/share/pipx`` on Linux) as its default -- a host running
    either pipx generation, or one upgraded across the switch without ever
    setting ``PIPX_HOME``, must still be detected.
    """
    candidates: list[Path] = []
    pipx_home = env.get("PIPX_HOME")
    if pipx_home:
        candidates.append(Path(pipx_home) / "venvs")
    candidates.append(home / ".local" / "pipx" / "venvs")
    candidates.append(home / ".local" / "share" / "pipx" / "venvs")
    return candidates


def _looks_pipx_managed(install_path: Path, env: Mapping[str, str], home: Path) -> bool:
    """Whether *install_path* sits under a pipx-managed venv.

    Checked against every known pipx venvs root (see
    :func:`_pipx_candidate_venv_dirs`), then falls back to a bare
    ``/pipx/venvs/`` marker anywhere in the resolved path -- covers a
    relocated or distro-patched pipx whose venvs root matches neither known
    default.
    """
    for venvs_dir in _pipx_candidate_venv_dirs(env, home):
        try:
            resolved_venvs = venvs_dir.resolve()
        except OSError:
            resolved_venvs = venvs_dir
        if install_path == resolved_venvs or resolved_venvs in install_path.parents:
            return True
    return "/pipx/venvs/" in install_path.as_posix()


def _package_install_path() -> Path:
    """Resolved path to the installed ``xnatctl`` package's ``__init__.py``."""
    return Path(xnatctl.__file__).resolve()


def _is_uv_managed(install_path: Path, *, max_levels: int = 8) -> bool:
    """Cheap check: does the venv's ``pyvenv.cfg`` (if any) name uv as its creator?

    Walks up from the package's directory looking for ``pyvenv.cfg`` --
    typically a few levels up from ``site-packages`` -- and reads it directly
    rather than invoking anything, so this never shells out just to detect
    the install.
    """
    current = install_path.parent
    for _ in range(max_levels):
        cfg = current / "pyvenv.cfg"
        if cfg.exists():
            try:
                return "uv" in cfg.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                return False
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def detect_upgrade_method(
    *,
    env: Mapping[str, str] | None = None,
    frozen: bool | None = None,
    install_path: Path | None = None,
    dockerenv_path: Path | None = None,
    home: Path | None = None,
) -> UpgradeMethod:
    """Detect how this xnatctl was installed, and how to upgrade it.

    Every input is an injectable seam (tests never monkeypatch ``sys.frozen``
    or the real ``/.dockerenv`` in place of passing these):

    Args:
        env: Environment mapping to read ``PIPX_HOME`` from. Defaults to
            ``os.environ``.
        frozen: Overrides the ``sys.frozen`` check. Defaults to reading it.
        install_path: Overrides the installed package's own path (used for
            the pipx/uv checks). Defaults to :func:`_package_install_path`.
        dockerenv_path: Overrides the path checked for a Docker container.
            Defaults to :data:`DOCKERENV_PATH`.
        home: Overrides the home directory the pipx default-venvs-root
            checks are built from. Defaults to ``Path.home()``.
    """
    active_env = env if env is not None else os.environ
    is_frozen = frozen if frozen is not None else bool(getattr(sys, "frozen", False))
    if is_frozen:
        return UpgradeMethod("frozen", "standalone binary (PyInstaller)", None)

    path = install_path if install_path is not None else _package_install_path()
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path

    active_home = home if home is not None else Path.home()
    if _looks_pipx_managed(resolved_path, active_env, active_home):
        return UpgradeMethod("pipx", "pipx-managed install", ("pipx", "upgrade", "xnatctl"))

    docker_path = dockerenv_path if dockerenv_path is not None else DOCKERENV_PATH
    if docker_path.exists():
        return UpgradeMethod("docker", "Docker container", ("docker", "pull", GHCR_IMAGE))

    if _is_uv_managed(resolved_path):
        return UpgradeMethod(
            "uv", "uv-managed virtual environment", ("uv", "pip", "install", "-U", "xnatctl")
        )
    return UpgradeMethod(
        "pip",
        "pip-managed environment",
        (sys.executable, "-m", "pip", "install", "-U", "xnatctl"),
    )


# =============================================================================
# Frozen-binary self-replace
# =============================================================================


def _detect_os() -> str:
    """Mirror ``install.sh``'s ``detect_os()``."""
    plat = sys.platform
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "darwin"
    if plat.startswith("win"):
        return "windows"
    raise UpgradeError(f"Unsupported operating system for self-update: {plat}")


def _detect_arch() -> str:
    """Mirror ``install.sh``'s ``detect_arch()``."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise UpgradeError(f"Unsupported architecture for self-update: {machine}")


def _release_asset_name(os_name: str, arch: str) -> str:
    """Mirror ``install.sh``'s ``ASSET`` naming: matches the ``binary`` CI job."""
    ext = "zip" if os_name == "windows" else "tar.gz"
    return f"xnatctl-{os_name}-{arch}.{ext}"


def fetch_latest_release_tag(timeout: float = CHECK_FETCH_TIMEOUT_SECONDS) -> str | None:
    """Fetch the latest GitHub release tag (e.g. ``"v0.5.0"``). None on any failure.

    Deliberately catches every exception, for the same reason
    ``update_check._fetch_latest_version`` does: this call crosses DNS, TLS,
    HTTP, and JSON parsing, and the contract is "return the tag, or None --
    never raise."
    """
    try:
        request = urllib.request.Request(
            GITHUB_LATEST_RELEASE_API,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.load(response)
        tag = payload["tag_name"]
    except Exception as e:  # noqa: BLE001 -- same silence contract as update_check
        logger.debug("Could not fetch the latest release tag: %s", e)
        return None
    return tag if isinstance(tag, str) else None


def _download_to(url: str, dest: Path, *, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> None:
    """Download *url* to *dest*. Raises :class:`UpgradeError` on any failure."""
    try:
        with (
            urllib.request.urlopen(url, timeout=timeout) as response,  # noqa: S310
            open(dest, "wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
    except (OSError, urllib.error.URLError) as e:
        raise UpgradeError(f"Could not download {url}: {e}") from e


def _parse_sha256_sidecar(text: str) -> str:
    """Extract the hex digest from a ``sha256sum``-format sidecar file.

    The release job writes these with plain ``sha256sum`` on Linux/macOS and
    an equivalent ``<hash>  <filename>`` line on Windows (see ``ci.yml``'s
    ``binary`` job and ``install.sh``, which parses the same shape) -- so
    just the first whitespace-separated token is the digest.
    """
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    tokens = first_line.split()
    digest = tokens[0] if tokens else ""
    if len(digest) != 64:
        raise UpgradeError(f"Malformed checksum file: {text!r}")
    return digest.lower()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_binary(archive: Path, os_name: str, dest_dir: Path) -> Path:
    """Extract the ``xnatctl`` executable from the downloaded archive."""
    binary_name = "xnatctl.exe" if os_name == "windows" else "xnatctl"
    if os_name == "windows":
        with zipfile.ZipFile(archive) as zf:
            zf.extract(binary_name, dest_dir)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            # The 3.12 "data" extraction filter, applied via the officially
            # documented backward-compatible attribute (not the extract()
            # `filter=` kwarg, which only accepts a string from 3.12 --
            # target is py311+): strips absolute paths/symlink escapes from
            # a tarball this code does not control the contents of.
            tf.extraction_filter = getattr(tarfile, "data_filter", None)
            tf.extract(binary_name, dest_dir)
    extracted = dest_dir / binary_name
    if not extracted.exists():
        raise UpgradeError(f"Downloaded archive did not contain {binary_name!r}")
    return extracted


def _verify_binary(path: Path, *, timeout: float = 10.0) -> None:
    """Run ``path --version`` as a sanity check. Raises :class:`UpgradeError` on failure.

    Mirrors ``install.sh``'s own post-install smoke test (its ``version_output``
    check). A checksum match only proves the downloaded bytes weren't
    corrupted in transit -- it says nothing about whether the binary actually
    RUNS (wrong OS/arch/libc, a truncated archive member, a bad extraction).
    Running ``--version`` is the cheapest real proof of life, used both
    BEFORE the candidate is installed over ``target`` and again AFTER, to
    confirm the installed copy still works.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [str(path), "--version"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpgradeError(f"{path} failed its --version sanity check: {e}") from e
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise UpgradeError(
            f"{path} failed its --version sanity check (exit {result.returncode}): {stderr}"
        )


def _restore_from_backup(target: Path, backup: Path, os_name: str) -> None:
    """Roll back to the pre-upgrade binary after a failed post-install verify.

    On Windows the failed install is a fresh file nobody has open, so it can
    simply be removed before the backup is renamed back into place. On POSIX
    ``backup`` is a standalone copy (see :func:`replace_frozen_binary`), so
    ``os.replace`` atomically restores it and consumes the backup in one step
    -- the same guarantee the forward install used.
    """
    if os_name == "windows":
        if target.exists():
            target.unlink()
        os.rename(backup, target)
    else:
        os.replace(backup, target)


def replace_frozen_binary(
    target: Path,
    *,
    fetch_release_tag: Callable[[], str | None] = fetch_latest_release_tag,
    download: Callable[[str, Path], None] = _download_to,
    verify: Callable[[Path], None] = _verify_binary,
) -> str:
    """Download, verify, and atomically install the latest release over *target*.

    Returns the installed release tag. Raises :class:`UpgradeError` on any
    failure. Download, checksum verification, and extraction all happen in a
    temp directory created as a *sibling* of ``target`` (``dir=target.parent``),
    which guarantees the new binary lands on the same filesystem as ``target``
    so installing it is a real atomic rename rather than a cross-device copy.

    Two ``--version`` sanity checks (via ``verify``) bracket the install: the
    extracted candidate is run BEFORE anything touches ``target`` (a valid
    checksum does not prove the binary actually runs), and the installed copy
    is run again AFTER -- if that second check fails, the pre-upgrade binary
    is restored automatically from a kept ``.bak`` and the failure is raised
    with that fact stated explicitly.

    The install step itself differs by platform: POSIX allows overwriting a
    running executable (the old inode stays valid via any process that
    already has it open), so it is copied aside to ``.bak`` and ``target`` is
    ``os.replace``-d directly, with ``.bak`` removed once the post-install
    verify succeeds. Windows locks a running ``.exe`` against overwrite or
    delete, so the pre-upgrade binary is instead RENAMED aside (renaming an
    open file is legal on Windows; deleting or overwriting it is not) and the
    new binary is moved into ``target``'s now-free name -- the ``.bak`` this
    leaves behind can only be removed once this process exits, which
    :func:`cleanup_stale_backup` does on the next launch.
    """
    tag = fetch_release_tag()
    if tag is None:
        raise UpgradeError("Could not determine the latest release from GitHub.")

    os_name = _detect_os()
    arch = _detect_arch()
    asset_name = _release_asset_name(os_name, arch)
    base_url = f"{GITHUB_RELEASE_DOWNLOAD_BASE}/{tag}"

    with tempfile.TemporaryDirectory(dir=target.parent) as tmp_str:
        tmp_dir = Path(tmp_str)
        archive_path = tmp_dir / asset_name
        checksum_path = tmp_dir / f"{asset_name}.sha256"

        download(f"{base_url}/{asset_name}", archive_path)
        download(f"{base_url}/{asset_name}.sha256", checksum_path)

        expected = _parse_sha256_sidecar(checksum_path.read_text(encoding="utf-8"))
        actual = _sha256_of(archive_path)
        if actual != expected:
            raise UpgradeError(
                f"Checksum mismatch for {asset_name}: expected {expected}, got {actual}. "
                f"{target} was not touched."
            )

        new_binary = _extract_binary(archive_path, os_name, tmp_dir)
        if os_name != "windows":
            os.chmod(new_binary, 0o755)

        # Proof the candidate actually runs, BEFORE target is touched at all.
        verify(new_binary)

        backup = target.with_name(target.name + ".bak")
        if backup.exists():
            # A previous run's backup that was never cleaned up (interrupted,
            # or -- on Windows -- deliberately deferred; see
            # cleanup_stale_backup) must not block this one.
            backup.unlink()

        if os_name == "windows":
            os.rename(target, backup)
            os.replace(new_binary, target)
        else:
            shutil.copy2(target, backup)
            try:
                os.replace(new_binary, target)
            except OSError as e:
                raise UpgradeError(
                    f"Could not install the new binary over {target}: {e}. The previous "
                    f"version is unchanged; a backup copy is also at {backup}."
                ) from e

        try:
            verify(target)
        except UpgradeError as verify_error:
            try:
                _restore_from_backup(target, backup, os_name)
            except OSError as restore_error:
                raise UpgradeError(
                    f"The installed binary at {target} failed its post-install "
                    f"--version check ({verify_error}), AND restoring the previous "
                    f"version from {backup} also failed ({restore_error}). Manual "
                    f"recovery needed: replace {target} with {backup}."
                ) from restore_error
            raise UpgradeError(
                f"The installed binary at {target} failed its post-install --version "
                f"check ({verify_error}); restored the previous version from {backup}."
            ) from verify_error

        if os_name != "windows":
            backup.unlink(missing_ok=True)
        # else: left in place -- cleanup_stale_backup() removes it on a later
        # launch, once nothing has the old binary open any more.

    return tag


def cleanup_stale_backup(target: Path | None = None) -> None:
    """Remove a leftover ``.bak`` from a previous frozen-binary upgrade.

    Mainly matters on Windows: :func:`replace_frozen_binary` cannot delete
    the pre-upgrade binary there -- the running process still has it open
    under the ``.bak`` name -- so it leaves that file for the NEXT launch (a
    fresh process, holding nothing open) to remove. Also covers a POSIX run
    killed between a successful replace and its own ``backup.unlink()``.
    Cheap and silent: meant to be called unconditionally from the frozen
    startup path (see ``cli/main.py``), so any failure (still locked,
    already gone, permissions) must never block a normal command.
    """
    exe = target if target is not None else Path(sys.executable)
    backup = exe.with_name(exe.name + ".bak")
    try:
        if backup.exists():
            backup.unlink()
    except OSError as e:
        logger.debug("Could not remove stale upgrade backup %s: %s", backup, e)


def _run_frozen_upgrade(*, execute: bool) -> None:
    """Implement the frozen-binary branch of `xnatctl upgrade`."""
    target = Path(sys.executable).resolve()

    if not execute:
        click.echo("Detected install method: standalone binary (PyInstaller).")
        click.echo(f"Would download the latest release from GitHub and replace: {target}")
        click.echo("Pass --yes/-y to perform the upgrade.", err=True)
        return

    if not os.access(target.parent, os.W_OK):
        print_error(f"{target.parent} is not writable -- cannot self-update.")
        print_hint(
            f"Reinstall manually: see https://github.com/{GITHUB_REPO}#installation, "
            f"or re-run with write access to {target.parent}."
        )
        raise SystemExit(ExitCode.PERMISSION_ERROR)

    tag = replace_frozen_binary(target)
    print_success(f"Upgraded {target} to {tag}.")


# =============================================================================
# --check
# =============================================================================


def _run_check() -> None:
    """Implement `xnatctl upgrade --check`.

    Unlike the passive per-command notice (``check_for_update``, which only
    ever reads the local cache), this does a real synchronous fetch -- that's
    the point of asking explicitly. ``refresh_cache_now`` is the same
    fetch-then-persist helper the detached background refresh is built on,
    just called inline and with a longer timeout.
    """
    latest_raw = update_check.refresh_cache_now(timeout=CHECK_FETCH_TIMEOUT_SECONDS)
    if latest_raw is None:
        print_error("Could not reach PyPI to check for updates.")
        raise SystemExit(ExitCode.NETWORK_ERROR)

    try:
        latest_version = Version(latest_raw)
        current_version = Version(xnatctl.__version__)
    except InvalidVersion:
        print_error("PyPI returned an unrecognizable version; could not compare.")
        raise SystemExit(ExitCode.NETWORK_ERROR) from None

    if latest_version > current_version:
        click.echo(
            f"A newer xnatctl release is available: {xnatctl.__version__} -> {latest_version}."
        )
        click.echo("Run 'xnatctl upgrade' to update.")
    else:
        print_success(f"xnatctl is up to date ({xnatctl.__version__}).")


# =============================================================================
# Command
# =============================================================================


@click.command("upgrade")
@click.option(
    "--check",
    is_flag=True,
    help="Check PyPI for a newer version and exit. Does not upgrade.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Actually perform the upgrade (default: dry run -- print the detected "
    "method and command only).",
)
@global_options
@handle_errors
def upgrade(ctx: Context, check: bool, yes: bool) -> None:
    """Detect how xnatctl was installed and update it in place.

    Dry by default: prints the detected install method and the exact command
    that would run. Pass --yes/-y to actually run it. A Docker install is
    never subprocessed regardless of --yes -- the printed `docker pull`
    command is the only output, since this process cannot replace the image
    it is running in.

    All output is human-oriented prose meant for a terminal, not a stable
    format to parse. For scripting, `--check`'s exit code is the machine
    interface: 0 for both up-to-date and a newer version being available,
    nonzero when PyPI could not be reached.
    """
    del ctx  # No client/auth needed -- detection and execution are both local.

    if check:
        _run_check()
        return

    method = detect_upgrade_method()

    if method.kind == "docker":
        click.echo(f"Detected install method: {method.description}.")
        click.echo(f"Run: {method.render_command()}")
        return

    if method.kind == "frozen":
        _run_frozen_upgrade(execute=yes)
        return

    click.echo(f"Detected install method: {method.description}.")
    click.echo(f"Command: {method.render_command()}")
    if not yes:
        click.echo("Dry run -- pass --yes/-y to run this command.", err=True)
        return

    command = method.command
    assert command is not None
    result = subprocess.run(list(command), check=False)  # noqa: S603
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print_success("Upgrade command completed.")
