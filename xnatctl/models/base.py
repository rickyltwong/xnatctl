"""Base model with common fields for all XNAT resources."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field


class BaseModel(PydanticBaseModel):
    """Base model with common configuration."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert model to dictionary."""
        return self.model_dump(exclude_none=True)

    def to_row(self, columns: list[str]) -> dict[str, Any]:
        """Convert model to row dict for table output."""
        data = self.to_dict()
        return {col: data.get(col, "") for col in columns}


class XNATResource(BaseModel):
    """Base model for XNAT resources with common fields."""

    id: str = Field(..., alias="ID", description="XNAT resource ID")
    label: Optional[str] = Field(None, description="Human-readable label")
    uri: Optional[str] = Field(None, alias="URI", description="REST API URI")
    xsi_type: Optional[str] = Field(None, alias="xsiType", description="XSI type")
    insert_date: Optional[datetime] = Field(
        None, alias="insert_date", description="Creation timestamp"
    )
    insert_user: Optional[str] = Field(
        None, alias="insert_user", description="User who created resource"
    )

    @property
    def display_id(self) -> str:
        """Return label if available, otherwise ID."""
        return self.label or self.id
