"""Project model for XNAT projects."""

from __future__ import annotations

from pydantic import Field

from .base import XNATResource


class Project(XNATResource):
    """XNAT project resource."""

    name: str | None = Field(None, description="Project name")
    secondary_id: str | None = Field(None, alias="secondary_ID", description="Secondary identifier")
    description: str | None = Field(None, description="Project description")
    pi_firstname: str | None = Field(None, alias="pi_firstname", description="PI first name")
    pi_lastname: str | None = Field(None, alias="pi_lastname", description="PI last name")
    accessibility: str | None = Field(None, description="Access level (public, protected, private)")
    subject_count: int | None = Field(None, alias="subjects", description="Number of subjects")
    session_count: int | None = Field(
        None, alias="experiments", description="Number of sessions/experiments"
    )

    @property
    def pi(self) -> str:
        """Return formatted PI name."""
        parts = []
        if self.pi_firstname:
            parts.append(self.pi_firstname)
        if self.pi_lastname:
            parts.append(self.pi_lastname)
        return " ".join(parts) if parts else ""

    @classmethod
    def table_columns(cls) -> list[str]:
        """Return columns for table output."""
        return ["id", "name", "pi", "subject_count", "session_count", "accessibility"]

    def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
        """Convert to row for table output."""
        cols = columns or self.table_columns()
        data = self.to_dict()
        data["pi"] = self.pi
        return {col: str(data.get(col, "")) for col in cols}
