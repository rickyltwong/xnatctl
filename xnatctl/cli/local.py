"""Local file operations for xnatctl (no XNAT connection required)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import click

from xnatctl.cli.common import handle_errors
from xnatctl.core.output import print_error, print_success


@click.group()
def local() -> None:
    """Local file operations (no XNAT connection required)."""
    pass


@local.command("extract")
@click.argument("input_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--cleanup/--no-cleanup", default=True, help="Remove ZIPs after extraction")
@click.option("--recursive", "-r", is_flag=True, help="Process subdirectories")
@click.option("--dry-run", is_flag=True, help="Preview what would be extracted")
@handle_errors
def local_extract(input_dir: str, cleanup: bool, recursive: bool, dry_run: bool) -> None:  # noqa: C901  # pre-existing; see pyproject
    """Extract downloaded XNAT session ZIPs.

    This command extracts ZIP files from previously downloaded sessions,
    creating organized subdirectories. Use after downloading without --extract,
    or to re-process existing downloads.

    \b
    Example:
        # Extract a single session directory
        xnatctl local extract ./data/XNAT_E00001

        # Extract all sessions, keeping ZIPs
        xnatctl local extract ./data --recursive --no-cleanup

        # Preview extraction
        xnatctl local extract ./data --recursive --dry-run
    """
    input_path = Path(input_dir)

    # Find ZIP files
    if recursive:
        zip_files = list(input_path.rglob("*.zip"))
    else:
        zip_files = list(input_path.glob("*.zip"))

    if not zip_files:
        click.echo("No ZIP files found.", err=True)
        return

    click.echo(f"Found {len(zip_files)} ZIP file(s)", err=True)

    if dry_run:
        click.echo("\n[DRY-RUN] Would extract:", err=True)
        for zip_file in zip_files:
            extract_dir = zip_file.parent / zip_file.stem
            click.echo(f"  {zip_file} -> {extract_dir}/", err=True)
            if cleanup:
                click.echo(f"    (would remove {zip_file.name})", err=True)
        return

    extracted = 0
    failed = 0

    for zip_path in zip_files:
        click.echo(f"Extracting {zip_path.name}...", err=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue

                    member_path = Path(member.filename)
                    if any(part.startswith(".") for part in member_path.parts):
                        continue

                    parts = member_path.parts
                    if len(parts) < 2:
                        continue

                    stripped_path = Path(*parts[1:])
                    output_path = zip_path.parent / stripped_path
                    # Guard against ZipSlip path traversal
                    if not output_path.resolve().is_relative_to(zip_path.parent.resolve()):
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(output_path, "wb") as target:
                        shutil.copyfileobj(source, target)

            extracted += 1

            if cleanup:
                zip_path.unlink()
                click.echo(f"  Removed {zip_path.name}", err=True)
        except zipfile.BadZipFile:
            print_error(f"Invalid ZIP file: {zip_path.name}")
            failed += 1
        except Exception as e:  # noqa: BLE001  # per-ZIP isolation in batch local-extract loop
            print_error(f"Failed to extract {zip_path.name}: {e}")
            failed += 1

    if failed:
        click.echo(f"\nExtracted: {extracted}, Failed: {failed}", err=True)
    else:
        print_success(f"Extracted {extracted} ZIP file(s)")
