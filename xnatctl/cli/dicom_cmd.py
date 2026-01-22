"""DICOM commands for xnatctl (optional, requires pydicom)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import click

from xnatctl.core.output import (
    print_error,
    print_json,
    print_success,
    print_table,
    print_warning,
)


def _normalize_errors(errors: object) -> list[str]:
    """Normalize error values to a list of strings."""
    if isinstance(errors, list):
        return [str(err) for err in errors]
    if errors:
        return [str(errors)]
    return []


def check_pydicom() -> bool:
    """Check if pydicom is available."""
    return importlib.util.find_spec("pydicom") is not None


@click.group()
def dicom() -> None:
    """DICOM utilities (requires pydicom).

    Install with: pip install xnatctl[dicom]
    """
    if not check_pydicom():
        print_warning("pydicom not installed. Install with: pip install xnatctl[dicom]")


@dicom.command("validate")
@click.argument("path", type=click.Path(exists=True))
@click.option("--recursive", "-r", is_flag=True, help="Search recursively")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
@click.option("--quiet", "-q", is_flag=True, help="Only output invalid files")
def dicom_validate(
    path: str,
    recursive: bool,
    output: str,
    quiet: bool,
) -> None:
    """Validate DICOM files.

    Example:
        xnatctl dicom validate /path/to/dicom
        xnatctl dicom validate /path/to/dicom -r
    """
    if not check_pydicom():
        print_error("pydicom not installed. Install with: pip install xnatctl[dicom]")
        raise SystemExit(1)

    import pydicom

    path_obj = Path(path)
    results = []

    # Collect files
    if path_obj.is_file():
        files = [path_obj]
    elif recursive:
        files = list(path_obj.rglob("*"))
    else:
        files = list(path_obj.glob("*"))

    files = [f for f in files if f.is_file()]

    valid_count = 0
    invalid_count = 0

    for file_path in files:
        try:
            # Try to read as DICOM
            ds = pydicom.dcmread(str(file_path), stop_before_pixels=True)

            # Basic validation checks
            errors = []
            warnings = []

            # Check required DICOM tags
            required_tags = [
                ("PatientID", 0x00100020),
                ("StudyInstanceUID", 0x0020000D),
                ("SeriesInstanceUID", 0x0020000E),
                ("SOPInstanceUID", 0x00080018),
                ("Modality", 0x00080060),
            ]

            for name, tag in required_tags:
                if tag not in ds:
                    errors.append(f"Missing required tag: {name}")

            # Check for private tags that might cause issues
            private_count = sum(1 for elem in ds if elem.tag.is_private)
            if private_count > 100:
                warnings.append(f"High number of private tags: {private_count}")

            is_valid = len(errors) == 0
            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1

            results.append(
                {
                    "file": str(file_path),
                    "valid": is_valid,
                    "errors": errors,
                    "warnings": warnings,
                    "patient_id": getattr(ds, "PatientID", ""),
                    "modality": getattr(ds, "Modality", ""),
                }
            )

        except pydicom.errors.InvalidDicomError:
            invalid_count += 1
            results.append(
                {
                    "file": str(file_path),
                    "valid": False,
                    "errors": ["Not a valid DICOM file"],
                    "warnings": [],
                }
            )
        except Exception as e:
            invalid_count += 1
            results.append(
                {
                    "file": str(file_path),
                    "valid": False,
                    "errors": [str(e)],
                    "warnings": [],
                }
            )

    if quiet:
        for r in results:
            if not r["valid"]:
                errors_list = _normalize_errors(r.get("errors"))
                click.echo(f"{r['file']}: {', '.join(errors_list)}")
    elif output == "json":
        print_json(
            {
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "files": results,
            }
        )
    else:
        click.echo(f"Validated {len(files)} files")
        click.echo(f"  Valid: {valid_count}")
        click.echo(f"  Invalid: {invalid_count}")

        if invalid_count > 0:
            click.echo("\nInvalid files:")
            for r in results:
                if not r["valid"]:
                    click.echo(f"  {r['file']}")
                    errors_list = _normalize_errors(r.get("errors"))
                    for err in errors_list:
                        click.echo(f"    - {err}")


@dicom.command("inspect")
@click.argument("file", type=click.Path(exists=True))
@click.option("--tag", "-t", multiple=True, help="Specific tags to show (e.g., PatientID)")
@click.option("--private", is_flag=True, help="Include private tags")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def dicom_inspect(
    file: str,
    tag: tuple[str, ...],
    private: bool,
    output: str,
) -> None:
    """Inspect DICOM file headers.

    Example:
        xnatctl dicom inspect /path/to/file.dcm
        xnatctl dicom inspect /path/to/file.dcm --tag PatientID --tag Modality
    """
    if not check_pydicom():
        print_error("pydicom not installed. Install with: pip install xnatctl[dicom]")
        raise SystemExit(1)

    import pydicom

    file_path = Path(file)

    try:
        ds = pydicom.dcmread(str(file_path), stop_before_pixels=True)
    except pydicom.errors.InvalidDicomError as e:
        print_error(f"Not a valid DICOM file: {file}")
        raise SystemExit(1) from e
    except Exception as e:
        print_error(f"Error reading file: {e}")
        raise SystemExit(1) from e

    # Build header dict
    headers = {}

    if tag:
        # Only show specified tags
        for t in tag:
            if hasattr(ds, t):
                value = getattr(ds, t)
                headers[t] = str(value)
            else:
                headers[t] = "(not found)"
    else:
        # Show common tags
        common_tags = [
            "PatientID",
            "PatientName",
            "PatientBirthDate",
            "PatientSex",
            "StudyInstanceUID",
            "StudyDate",
            "StudyTime",
            "StudyDescription",
            "SeriesInstanceUID",
            "SeriesNumber",
            "SeriesDescription",
            "SOPInstanceUID",
            "SOPClassUID",
            "Modality",
            "Manufacturer",
            "ManufacturerModelName",
            "InstitutionName",
            "StationName",
            "AccessionNumber",
        ]

        for t in common_tags:
            if hasattr(ds, t):
                value = getattr(ds, t)
                headers[t] = str(value)

        if private:
            for elem in ds:
                if elem.tag.is_private:
                    tag_str = f"{elem.tag.group:04X},{elem.tag.element:04X}"
                    headers[tag_str] = str(elem.value)[:100]  # Truncate long values

    if output == "json":
        print_json(headers)
    else:
        click.echo(f"File: {file}")
        click.echo("-" * 60)
        for key, value in headers.items():
            click.echo(f"{key}: {value}")


@dicom.command("list-tags")
@click.argument("file", type=click.Path(exists=True))
@click.option("--private", is_flag=True, help="Include private tags")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def dicom_list_tags(
    file: str,
    private: bool,
    output: str,
) -> None:
    """List all tags in a DICOM file.

    Example:
        xnatctl dicom list-tags /path/to/file.dcm
    """
    if not check_pydicom():
        print_error("pydicom not installed. Install with: pip install xnatctl[dicom]")
        raise SystemExit(1)

    import pydicom

    file_path = Path(file)

    try:
        ds = pydicom.dcmread(str(file_path), stop_before_pixels=True)
    except pydicom.errors.InvalidDicomError as e:
        print_error(f"Not a valid DICOM file: {file}")
        raise SystemExit(1) from e
    except Exception as e:
        print_error(f"Error reading file: {e}")
        raise SystemExit(1) from e

    tags = []
    for elem in ds:
        if not private and elem.tag.is_private:
            continue

        tag_str = f"({elem.tag.group:04X},{elem.tag.element:04X})"
        vr = elem.VR if hasattr(elem, "VR") else ""
        name = elem.keyword if hasattr(elem, "keyword") else ""
        value = str(elem.value)[:50] if elem.value else ""  # Truncate

        tags.append(
            {
                "tag": tag_str,
                "vr": vr,
                "name": name,
                "value": value,
            }
        )

    if output == "json":
        print_json(tags)
    else:
        columns = ["tag", "vr", "name", "value"]
        print_table(tags, columns, title=f"Tags in {file}")


@dicom.command("anonymize")
@click.argument("input_path", type=click.Path(exists=True))
@click.argument("output_path", type=click.Path())
@click.option("--patient-id", help="New patient ID")
@click.option("--patient-name", help="New patient name")
@click.option("--remove-private", is_flag=True, help="Remove private tags")
@click.option("--recursive", "-r", is_flag=True, help="Process directory recursively")
@click.option("--dry-run", is_flag=True, help="Preview without saving")
def dicom_anonymize(
    input_path: str,
    output_path: str,
    patient_id: str | None,
    patient_name: str | None,
    remove_private: bool,
    recursive: bool,
    dry_run: bool,
) -> None:
    """Anonymize DICOM files.

    Example:
        xnatctl dicom anonymize input.dcm output.dcm --patient-id ANON001
        xnatctl dicom anonymize /input/dir /output/dir -r --remove-private
    """
    if not check_pydicom():
        print_error("pydicom not installed. Install with: pip install xnatctl[dicom]")
        raise SystemExit(1)

    import pydicom

    input_obj = Path(input_path)
    output_obj = Path(output_path)

    # Collect files
    if input_obj.is_file():
        files = [(input_obj, output_obj)]
    elif recursive:
        files = []
        for f in input_obj.rglob("*"):
            if f.is_file():
                rel = f.relative_to(input_obj)
                files.append((f, output_obj / rel))
    else:
        files = []
        for f in input_obj.glob("*"):
            if f.is_file():
                files.append((f, output_obj / f.name))

    processed = 0
    skipped = 0

    for in_file, out_file in files:
        try:
            ds = pydicom.dcmread(str(in_file))

            # Anonymize
            if patient_id:
                ds.PatientID = patient_id
            if patient_name:
                ds.PatientName = patient_name

            # Remove identifying tags
            tags_to_remove = [
                "PatientBirthDate",
                "PatientAddress",
                "InstitutionName",
                "InstitutionAddress",
                "ReferringPhysicianName",
                "PerformingPhysicianName",
                "OperatorsName",
            ]

            for tag_name in tags_to_remove:
                if hasattr(ds, tag_name):
                    delattr(ds, tag_name)

            # Remove private tags if requested
            if remove_private:
                ds.remove_private_tags()

            if dry_run:
                click.echo(f"Would anonymize: {in_file} -> {out_file}")
            else:
                out_file.parent.mkdir(parents=True, exist_ok=True)
                ds.save_as(str(out_file))

            processed += 1

        except pydicom.errors.InvalidDicomError:
            skipped += 1
        except Exception as e:
            click.echo(f"Error processing {in_file}: {e}", err=True)
            skipped += 1

    if dry_run:
        click.echo(f"\nWould process {processed} files, skip {skipped} files")
    else:
        print_success(f"Processed {processed} files, skipped {skipped} files")
