"""Session/Experiment model for XNAT sessions."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from .base import XNATResource


class Session(XNATResource):
    """XNAT session/experiment resource."""

    project: str | None = Field(None, description="Parent project ID")
    subject_id: str | None = Field(None, alias="subject_ID", description="Parent subject ID")
    subject_label: str | None = Field(
        None, alias="subject_label", description="Parent subject label"
    )
    modality: str | None = Field(None, description="Imaging modality (MR, PET, CT)")
    session_type: str | None = Field(None, alias="session_type", description="Session type")
    acquisition_site: str | None = Field(
        None, alias="acquisition_site", description="Acquisition site"
    )
    scanner: str | None = Field(None, description="Scanner name")
    operator: str | None = Field(None, description="Operator name")
    dcm_accession_number: str | None = Field(
        None, alias="dcmAccessionNumber", description="DICOM accession number"
    )
    dcm_patient_id: str | None = Field(None, alias="dcmPatientId", description="DICOM patient ID")
    dcm_patient_name: str | None = Field(
        None, alias="dcmPatientName", description="DICOM patient name"
    )
    session_date: date | None = Field(None, alias="date", description="Session date")
    time: str | None = Field(None, description="Session time")
    age: int | None = Field(None, description="Subject age at scan")
    scan_count: int | None = Field(None, alias="scans", description="Number of scans")
    resource_count: int | None = Field(None, alias="resources", description="Number of resources")
    note: str | None = Field(None, description="Session notes")
    visit_id: str | None = Field(None, alias="visit_id", description="Visit ID")

    @classmethod
    def table_columns(cls) -> list[str]:
        """Return columns for table output."""
        return [
            "id",
            "label",
            "subject_label",
            "session_date",
            "modality",
            "scan_count",
        ]

    def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
        """Convert to row for table output."""
        cols = columns or self.table_columns()
        data = self.to_dict()
        if self.session_date:
            data["session_date"] = self.session_date.isoformat()
        return {col: str(data.get(col, "")) for col in cols}


# Alias for backward compatibility
Experiment = Session
