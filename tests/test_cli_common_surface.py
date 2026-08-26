"""Pin the public surface of the ``xnatctl.cli.common`` package.

Other CLI modules (and tests) reach for a handful of exceptions and helpers
by name from ``xnatctl.cli.common``, whose ``__init__`` re-exports them from
its submodules (a package, one submodule per concern). This test pins the
expected set explicitly so a split or refactor cannot drop (or silently add)
a name without the diff surfacing here.
"""

from __future__ import annotations

import xnatctl.cli.common as common

EXPECTED_PUBLIC_SURFACE = {
    "AUDIT_ERROR_KEY",
    "AuthenticationError",
    "Config",
    "ConfigurationError",
    "Context",
    "DEPRECATED_FLAGS",
    "DeprecatedFlag",
    "ExitCode",
    "FILE_ONLY_ATTR",
    "InputValidationError",
    "OperationCancelledError",
    "OutputFormat",
    "PermissionDeniedError",
    "Profile",
    "ProfileNotFoundError",
    "ResourceNotFoundError",
    "XNATClient",
    "XNATConnectionError",
    "XNATCtlError",
    "apply_filter",
    "apply_sort_limit",
    "batch_option",
    "confirm_destructive",
    "confirm_destructive_when",
    "create_dest_client",
    "debug_env_enabled",
    "default_project_from_context",
    "dest_profile_options",
    "deprecation_message",
    "exit_code_for",
    "get_credentials",
    "get_profile",
    "global_options",
    "handle_errors",
    "list_options",
    "parallel_options",
    "pass_context",
    "print_error",
    "print_hint",
    "read_password_stdin",
    "reject_argv_password",
    "reject_blank_option_value",
    "render_cli_error",
    "require_auth",
    "require_project_from_context",
    "resolve_archive_mode_from_context",
    "resolve_columns",
    "resolve_direct_archive_from_context",
    "resolve_overwrite_from_context",
    "resolve_workers_from_context",
    "validate_local_path_option_cb",
}


def test_all_matches_expected_public_surface() -> None:
    """``__all__`` is exactly the pinned set -- no silent drops or additions."""
    actual = set(common.__all__)
    missing = EXPECTED_PUBLIC_SURFACE - actual
    extra = actual - EXPECTED_PUBLIC_SURFACE
    assert not missing and not extra, (
        f"xnatctl.cli.common.__all__ drifted from the pinned public surface. "
        f"Missing (expected but not exported): {sorted(missing)}. "
        f"Extra (exported but not expected): {sorted(extra)}."
    )


def test_all_names_are_actually_importable() -> None:
    """Every name listed in ``__all__`` must resolve on the module -- ``__all__`` is a promise, not a wish."""
    missing = [name for name in common.__all__ if not hasattr(common, name)]
    assert not missing, (
        f"xnatctl.cli.common.__all__ lists names that are not bound on the module: {missing}."
    )


def test_all_excludes_private_helpers() -> None:
    """No underscore-prefixed helper is advertised as part of the public surface."""
    private = [name for name in common.__all__ if name.startswith("_")]
    assert not private, (
        f"xnatctl.cli.common.__all__ lists private helpers, which should stay "
        f"importable by dotted name but out of the public surface: {private}."
    )
