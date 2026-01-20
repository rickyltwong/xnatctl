"""Session/Experiment model for XNAT sessions."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import Field

from .base import XNATResource


class Session(XNATResource):
    """XNAT session/experiment resource."""

    project: Optional[str] = Field(None, description="Parent project ID")
    subject_id: Optional[str] = Field(
        None, alias="subject_ID", description="Parent subject ID"
    )
    subject_label: Optional[str] = Field(
        None, alias="subject_label", description="Parent subject label"
    )
    modality: Optional[str] = Field(None, description="Imaging modality (MR, PET, CT)")
    session_type: Optional[str] = Field(
        None, alias="session_type", description="Session type"
    )
    acquisition_site: Optional[str] = Field(
        None, alias="acquisition_site", description="Acquisition site"
    )
    scanner: Optional[str] = Field(None, description="Scanner name")
    operator: Optional[str] = Field(None, description="Operator name")
    dcm_accession_number: Optional[str] = Field(
        None, alias="dcmAccessionNumber", description="DICOM accession number"
    )
    dcm_patient_id: Optional[str] = Field(
        None, alias="dcmPatientId", description="DICOM patient ID"
    )
    dcm_patient_name: Optional[str] = Field(
        None, alias="dcmPatientName", description="DICOM patient name"
    )
    session_date: Optional[date] = Field(
        None, alias="date", description="Session date"
    )
    time: Optional[str] = Field(None, description="Session time")
    age: Optional[int] = Field(None, description="Subject age at scan")
    scan_count: Optional[int] = Field(
        None, alias="scans", description="Number of scans"
    )
    resource_count: Optional[int] = Field(
        None, alias="resources", description="Number of resources"
    )
    note: Optional[str] = Field(None, description="Session notes")
    visit_id: Optional[str] = Field(None, alias="visit_id", description="Visit ID")

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
