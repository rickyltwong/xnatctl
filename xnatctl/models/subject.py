"""Subject model for XNAT subjects."""

from __future__ import annotations

from pydantic import Field

from .base import XNATResource


class Subject(XNATResource):
    """XNAT subject resource."""

    project: str | None = Field(None, description="Parent project ID")
    group: str | None = Field(None, description="Subject group")
    src: str | None = Field(None, description="Source")
    gender: str | None = Field(None, description="Gender")
    handedness: str | None = Field(None, description="Handedness")
    yob: int | None = Field(None, description="Year of birth")
    dob: str | None = Field(None, description="Date of birth")
    education: str | None = Field(None, description="Education level")
    ses: str | None = Field(None, description="Socioeconomic status")
    race: str | None = Field(None, description="Race")
    ethnicity: str | None = Field(None, description="Ethnicity")
    session_count: int | None = Field(None, alias="experiments", description="Number of sessions")

    @classmethod
    def table_columns(cls) -> list[str]:
        """Return columns for table output."""
        return ["id", "label", "project", "session_count", "gender", "group"]

    def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
        """Convert to row for table output."""
        cols = columns or self.table_columns()
        data = self.to_dict()
        return {col: str(data.get(col, "")) for col in cols}
