"""Eager Click option-value callbacks shared across commands."""

from __future__ import annotations

from typing import Any

import click

from xnatctl.core.exceptions import InputValidationError
from xnatctl.core.validation import validate_local_path_component


def validate_local_path_option_cb(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    """Eager Click callback: reject an option value unsafe as a local path.

    Wire this as ``callback=validate_local_path_option_cb, is_eager=True`` on
    any option (like ``--name``) whose value becomes a local file/directory
    name. Eager callbacks run during argument parsing, in
    :meth:`click.Command.parse_args` -- before ``@require_auth`` or any other
    decorator wrapping the command body runs, since those only execute once
    Click calls the underlying function. Without ``is_eager=True`` the
    validation would still technically run before the command body, but
    only after every other (non-eager) option's callback -- eager forces it
    first, and more importantly decouples it from needing a valid session at
    all: a malformed ``--name`` must fail the same way whether or not the
    caller is authenticated.

    ``None`` (the option was not given) passes through unchanged; the
    command body is responsible for its own fallback validation in that case
    (e.g. validating the session ID that will be used as the directory name
    instead).
    """
    del ctx
    if value is not None:
        option = (param.opts or [param.name])[0]
        try:
            validate_local_path_component(value, option)
        except InputValidationError as e:
            raise click.ClickException(str(e)) from e
    return value


def reject_blank_option_value(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    """Click callback: reject an explicitly empty/whitespace-only option value.

    ``None`` (the option was not given) passes through unchanged -- only a
    user-supplied blank string is rejected. This matters most on an option
    that gates a ``confirm_destructive_when`` predicate written as
    ``is not None`` (present means mutating) alongside a command body that
    checks plain truthiness (``not value`` means absent): an empty string
    satisfies the first and fails the second, so the confirmation
    prompt/audit fires for what actually runs as a harmless read. Wiring
    this callback removes the blank state before either check ever sees it.
    """
    del ctx, param
    if value is not None and value.strip() == "":
        raise click.BadParameter("cannot be empty or whitespace-only")
    return value
