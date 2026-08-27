"""Profile-derived defaults resolved against the shared CLI :class:`Context`.

Each helper prefers the explicit option value, then the active profile's
setting, then a hardcoded default -- the "CLI args > env vars > profile
config" credential-priority rule applied to per-command options.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from xnatctl.core.config import Profile
from xnatctl.core.exceptions import ProfileNotFoundError

if TYPE_CHECKING:
    from xnatctl.cli.common import Context


def get_profile(ctx: Context) -> Profile | None:
    """Return the active profile, if configured and resolvable."""
    if ctx.config is None:
        return None

    try:
        return ctx.config.get_profile(ctx.profile_name)
    except ProfileNotFoundError:
        return None


def default_project_from_context(ctx: Context) -> str | None:
    """Return the profile default project if available."""
    profile = get_profile(ctx)
    return profile.default_project if profile else None


def require_project_from_context(ctx: Context, project: str | None) -> str:
    """Return an explicit or default project, or raise a Click error."""
    resolved_project = project or default_project_from_context(ctx)
    if resolved_project:
        return resolved_project

    profile_name = ctx.profile_name or (ctx.config.default_profile if ctx.config else "default")
    raise click.ClickException(
        f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
    )


def resolve_workers_from_context(ctx: Context, workers: int | None, default: int = 4) -> int:
    """Resolve worker count from explicit option, profile, or a default."""
    if workers is not None:
        return workers

    profile = get_profile(ctx)
    if profile and profile.workers is not None:
        return profile.workers

    return default


def resolve_overwrite_from_context(ctx: Context, overwrite: str | None) -> str:
    """Resolve overwrite mode from explicit option, profile, or ``delete``."""
    if overwrite is not None:
        return overwrite
    profile = get_profile(ctx)
    if profile and profile.overwrite is not None:
        return profile.overwrite
    return "delete"


def resolve_direct_archive_from_context(ctx: Context, direct_archive: bool | None) -> bool:
    """Resolve direct-archive flag from explicit option, profile, or ``True``."""
    if direct_archive is not None:
        return direct_archive
    profile = get_profile(ctx)
    if profile and profile.direct_archive is not None:
        return profile.direct_archive
    return True


def resolve_archive_mode_from_context(ctx: Context, mode: str | None) -> str:
    """Resolve archive mode from explicit option, profile, or ``tar``."""
    if mode is not None:
        return mode
    profile = get_profile(ctx)
    if profile and profile.archive_mode is not None:
        return profile.archive_mode
    return "tar"
