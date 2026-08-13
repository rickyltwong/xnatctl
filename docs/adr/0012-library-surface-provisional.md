# 0012. Library surface stays Provisional until the M3 refactors settle

- **Status:** Accepted
- **Date:** 2026-08-10

## Context

The Python package exports `XNATClient`, `Config`, `Profile`, and six
exceptions. Users who `import xnatctl` get a working client, but the service
layer it wraps is about to move: the engine extractions, retry unification, and
exception renames planned for the next milestone restructure method signatures,
module paths, and three exception class names that shadow stdlib (`ConnectionError`,
`TimeoutError`, `ValidationError`).

The question is whether to declare the library surface **Stable** now, which
would semver-govern an API about to change, or wait.

## Decision

The library surface stays **Provisional**, the tier `docs/stability.rst`
already assigns it. It will be promoted to Stable once the service-layer
restructuring and exception renames are complete and the public facade is
settled — not before.

Provisional means: the modules are documented, importable, and intentional, but
names and signatures may move between minor releases. Pin an exact version if
you import them.

## Consequences

Library consumers get a real, documented contract ("pin an exact version, expect
movement") while the project retains the freedom to restructure without forcing
a major bump or deferring refactors indefinitely. Once the facade, service-layer
routing, and exception renames land, promoting to Stable is a one-line doc
change with no code impact.
