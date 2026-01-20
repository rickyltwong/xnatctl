"""Subject model for XNAT subjects."""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from .base import XNATResource


class Subject(XNATResource):
    """XNAT subject resource."""

    project: Optional[str] = Field(None, description="Parent project ID")
    group: Optional[str] = Field(None, description="Subject group")
    src: Optional[str] = Field(None, description="Source")
    gender: Optional[str] = Field(None, description="Gender")
    handedness: Optional[str] = Field(None, description="Handedness")
    yob: Optional[int] = Field(None, description="Year of birth")
    dob: Optional[str] = Field(None, description="Date of birth")
    education: Optional[str] = Field(None, description="Education level")
    ses: Optional[str] = Field(None, description="Socioeconomic status")
    race: Optional[str] = Field(None, description="Race")
    ethnicity: Optional[str] = Field(None, description="Ethnicity")
    session_count: Optional[int] = Field(
        None, alias="experiments", description="Number of sessions"
    )

    @classmethod
    def table_columns(cls) -> list[str]:
        """Return columns for table output."""
        return ["id", "label", "project", "session_count", "gender", "group"]

    def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
        """Convert to row for table output."""
        cols = columns or self.table_columns()
        data = self.to_dict()
        return {col: str(data.get(col, "")) for col in cols}
