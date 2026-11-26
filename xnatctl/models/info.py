"""Typed results for client-level introspection (ping, whoami).

Unlike the resource models, these are not XNAT ResultSet rows: ``ServerInfo``
is synthesized by :meth:`XNATClient.ping` and ``UserInfo`` normalizes the
``/xapi/users/{username}`` payload. Both are frozen snapshots -- there is
nothing to mutate about "what the server said at that moment".
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator

from .base import BaseModel


class ServerInfo(BaseModel):
    """Server reachability and version, as reported by :meth:`XNATClient.ping`."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(..., description="Server base URL")
    status: str = Field(..., description="Reachability status ('ok' when the ping succeeded)")
    version: str | None = Field(
        default=None, description="XNAT build version, when the server reports one"
    )
    latency_ms: int = Field(..., description="Round-trip latency of the ping in milliseconds")


class UserInfo(BaseModel):
    """The authenticated user's account details, from :meth:`XNATClient.whoami`.

    Field aliases mirror the ``/xapi/users/{username}`` wire keys
    (``firstName``/``lastName``), so ``model_validate`` accepts the raw payload
    and ``model_dump(by_alias=True)`` reproduces it; the plain field names
    match the whoami keys the CLI renders, so ``model_dump()`` matches the
    stable CLI output shape.
    """

    model_config = ConfigDict(frozen=True)

    username: str = Field(..., description="Account username")
    firstname: str = Field(default="", alias="firstName", description="First name")
    lastname: str = Field(default="", alias="lastName", description="Last name")
    email: str = Field(default="", description="Email address")
    enabled: bool = Field(default=False, description="Whether the account is enabled")

    @field_validator("firstname", "lastname", "email", mode="before")
    @classmethod
    def _blank_unusable_optional_str(cls, value: object) -> object:
        return value if isinstance(value, str) else ""
