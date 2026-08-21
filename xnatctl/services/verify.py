"""Local verification of downloaded files against server-reported checksums.

Pure path-keying and hashing logic, deliberately free of any HTTP dependency --
:class:`~xnatctl.services.downloads.DownloadService` fetches the server
manifest and calls into this module to compare it against what actually landed
on disk.

Every keying function here is anchored to what its *source* structurally
guarantees, rather than searching a path for a marker token. A generic search
for a literal ``"scans"``/``"resources"``/``"files"`` token is ambiguous the
moment one of those words is also a legitimate resource label or shows up
inside a file's own relative name -- anchoring at a fixed position (and never
looking past it) makes that ambiguity impossible instead of merely unlikely.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path

from xnatctl.models.progress import VerificationReport

_HASH_CHUNK_SIZE = 1024 * 1024

# `_extract_scan_zip` renames a freshly downloaded file to `X__dup1` (then
# `__dup2`, ...) when `X` already exists on disk -- a purely local disk
# collision-avoidance step the server knows nothing about, so the server's
# manifest references the un-renamed name UNLESS a server file is genuinely
# named that way (rare, but real -- see `_fold_local_dups`).
_DUP_SUFFIX_RE = re.compile(r"^(?P<stem>.*)__dup(?P<n>\d+)(?P<suffix>\.[^./]*)?$")


def _scan_tree_key(parts: tuple[str, ...]) -> str | None:
    """(a) Anchored at position 0: ``scans/{scan_id}/resources/{label}/files/{name...}``.

    This is the shape a session download's extracted scan tree uses
    (``session_dir`` is the root), and -- after skipping one leading
    wrapper segment -- the shape a scan ZIP's own members use too; see
    :func:`scan_source_key`.
    """
    if len(parts) > 5 and parts[0] == "scans" and parts[2] == "resources" and parts[4] == "files":
        return f"scans/{parts[1]}/resources/{parts[3]}/{'/'.join(parts[5:])}"
    return None


def _scan_tree_prescoped_key(parts: tuple[str, ...], resource_label: str) -> str | None:
    """The pre-scoped shape a single-resource download's server ZIP may use.

    ``scans/{scan_id}/files/{name}`` or ``scans/{scan_id}/{label}/files/{name}``,
    anchored at position 0 -- the ``resources/{label}`` marker omitted (the
    live shape is unconfirmed, so both are accepted).
    """
    if len(parts) < 3 or parts[0] != "scans":
        return None
    rest = parts[2:]
    if rest[:1] == ("files",) and len(rest) > 1:
        return f"scans/{parts[1]}/resources/{resource_label}/{'/'.join(rest[1:])}"
    if len(rest) > 2 and rest[1] == "files":
        return f"scans/{parts[1]}/resources/{resource_label}/{'/'.join(rest[2:])}"
    return None


def scan_source_key(
    parts: Sequence[str], *, wrapped: bool, resource_label: str | None = None
) -> str | None:
    """Key a scan-level source: an extracted scan tree, or a scan ZIP's members.

    Anchored deterministically at the position the *source* is known to
    use -- position 0, or position 1 with one leading segment skipped --
    never probed by trying both and taking whichever happens to parse. A
    session-label wrapper that is itself literally ``"scans"`` (or a
    pathological numeric-looking id) can make both anchors parse at once;
    letting the caller declare *wrapped* from what it already knows about
    its own source, rather than guessing, is what keeps that from picking
    the wrong one silently.

    *wrapped* is True for every scan-level ZIP (XNAT always wraps ZIP
    entries in a session/experiment-label directory) and for
    ``scan download --extract``'s tree (its raw ZIP member paths land
    unstripped under an artificial ``scans/`` extraction root). It is False
    for a session download's own extracted tree, written directly by
    ``_extract_scan_zip``/``extract_session_zips`` with no such wrapper.

    Never searched further than the fixed positions this checks: a
    ``"scans"``/``"resources"`` token appearing later -- inside the opaque
    name, or because a resource is itself labeled ``"scans"`` -- is never
    revisited or misread as a second marker.
    """
    candidate = tuple(parts)[1:] if wrapped else tuple(parts)
    key = _scan_tree_key(candidate)
    if key is not None:
        return key
    if resource_label is not None:
        return _scan_tree_prescoped_key(candidate, resource_label)
    return None


def session_resource_zip_member_keys(parts: Sequence[str], *, label: str) -> tuple[str, ...]:
    """(c) Candidate key(s) for a session-resource ZIP's own member, in priority order.

    The ZIP is already scoped to exactly one resource by the caller (its own
    filename carries the label -- ``resources_{label}.zip``), so *label* is
    never guessed -- only located. XNAT resource ZIPs are documented (see
    ``services/transfer/executor.py::_strip_xnat_prefix``) to carry the full
    hierarchy ``{session_label}/resources/{label}/files/{name}``; a bare
    ``{label}/{name}`` or a flat ``{name}`` with no wrapper at all are also
    accepted, since the exact shape a given server emits isn't guaranteed.

    Up to three shapes are recognized, each contributing (at most) one
    candidate, in this priority order:

    1. Full-hierarchy: the ``resources/{label}/files/`` marker accepted ONLY
       at its structural position -- exactly one leading segment before it
       (``parts[1] == "resources"``). A marker appearing deeper is payload
       content, never structure, and is never matched.
    2. Bare, label-prefixed: the member path starts with ``{label}/`` --
       strip that one segment.
    3. Flat: the whole member path is the name, unchanged. Always included,
       since (1) and (2) both describe wrapper shapes that might not apply.

    A member matching shape 1 positionally is not automatically the right
    answer, though: its own leading segment might not be a wrapper at all,
    but genuine payload content whose second component happens to be the
    literal word ``"resources"`` followed by this very label (e.g. a flat
    member named ``nested/resources/QC/files/report.txt`` for resource
    ``QC``). Positional shape alone cannot tell these apart -- see
    :func:`verify_manifest`, which resolves the ambiguity against the server
    manifest (ground truth for which name the file actually has).
    """
    fixed = tuple(parts)
    candidates: list[str] = []

    if len(fixed) > 4 and fixed[1] == "resources" and fixed[2] == label and fixed[3] == "files":
        candidates.append(f"resources/{label}/{'/'.join(fixed[4:])}")

    if fixed[:1] == (label,) and len(fixed) > 1:
        stripped = f"resources/{label}/{'/'.join(fixed[1:])}"
        if stripped not in candidates:
            candidates.append(stripped)

    flat = f"resources/{label}/{'/'.join(fixed)}"
    if flat not in candidates:
        candidates.append(flat)

    return tuple(candidates)


def key_from_uri(parts: Sequence[str]) -> str | None:
    """(d) A server file listing's ``URI``, parsed positionally.

    Anchored on the canonical form XNAT emits --
    ``/data/(experiments/{id}|projects/{p}[/subjects/{s}]/experiments/{id})/
    [scans/{scan_id}/]resources/{label}/files/{name...}`` -- rather than
    searched: a resource literally labeled ``"scans"`` sits at the fixed
    label position and is never mistaken for the scan-id marker, because by
    the time that position is read, the marker itself has already been
    matched positionally.
    """
    fixed = tuple(parts)
    try:
        idx = fixed.index("data")
    except ValueError:
        return None
    rest = fixed[idx + 1 :]

    if rest[:1] == ("projects",) and len(rest) > 1:
        rest = rest[2:]
        if rest[:1] == ("subjects",) and len(rest) > 1:
            rest = rest[2:]
    if rest[:1] != ("experiments",) or len(rest) < 2:
        return None
    rest = rest[2:]

    scan_id: str | None = None
    if rest[:1] == ("scans",) and len(rest) > 1:
        scan_id = rest[1]
        rest = rest[2:]

    if rest[:1] != ("resources",) or len(rest) < 4 or rest[2] != "files":
        return None
    label = rest[1]
    name = "/".join(rest[3:])
    if not name:
        return None
    if scan_id is not None:
        return f"scans/{scan_id}/resources/{label}/{name}"
    return f"resources/{label}/{name}"


def _canonical_key_and_rank(key: str) -> tuple[str, int]:
    """Strip an `_extract_scan_zip`-style ``__dupN`` marker from a key's final segment.

    Returns ``(key, 0)`` unchanged when there is no such marker.
    """
    head, _, name = key.rpartition("/")
    match = _DUP_SUFFIX_RE.match(name)
    if match is None:
        return key, 0
    canonical_name = f"{match.group('stem')}{match.group('suffix') or ''}"
    canonical_key = f"{head}/{canonical_name}" if head else canonical_name
    return canonical_key, int(match.group("n"))


def _fold_local_dups(
    local_index: dict[str, Path], manifest: dict[str, str | None], collisions: set[str]
) -> dict[str, Path]:
    """Fold local ``__dupN`` keys the manifest doesn't know about onto their canonical key.

    A literal ``X__dupN`` key present in the *manifest* is a real server
    file -- left indexed under its own name, untouched. Otherwise it can only
    be `_extract_scan_zip`'s local rename of a fresh re-download of ``X``, so
    it is folded onto canonical key ``X``, keeping whichever dup has the
    highest rank (the file the most recent download actually produced).
    """
    folded = dict(local_index)
    dup_candidates: dict[str, list[tuple[int, Path]]] = {}
    for key, path in local_index.items():
        canonical_key, rank = _canonical_key_and_rank(key)
        if rank == 0 or key in manifest:
            continue
        dup_candidates.setdefault(canonical_key, []).append((rank, path))
        del folded[key]

    for canonical_key, candidates in dup_candidates.items():
        if canonical_key in collisions:
            continue
        _rank, best_path = max(candidates, key=lambda candidate: candidate[0])
        folded[canonical_key] = best_path

    return folded


def _read_chunks(chunks: Iterator[bytes]) -> str:
    digest = hashlib.md5()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def _md5_file(path: Path) -> str:
    with open(path, "rb") as f:
        return _read_chunks(iter(lambda: f.read(_HASH_CHUNK_SIZE), b""))


def _md5_zip_member(zf: zipfile.ZipFile, member: str) -> str:
    with zf.open(member) as f:
        return _read_chunks(iter(lambda: f.read(_HASH_CHUNK_SIZE), b""))


def _index_local_tree(
    root: Path, *, resource_label: str | None, wrapped: bool
) -> tuple[dict[str, Path], list[str]]:
    """Index an extracted scan tree by :func:`scan_source_key`.

    Two different, unrelated files mapping to the same key are reported as
    collisions and excluded from the index (never silently last-write-wins).
    ``__dupN`` folding is deliberately NOT done here: it needs the server
    manifest to tell a real dup-named server file from a local rename
    artifact, so it happens in :func:`verify_manifest` instead.
    """
    raw: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        key = scan_source_key(
            path.relative_to(root).parts, wrapped=wrapped, resource_label=resource_label
        )
        if key is not None:
            raw.setdefault(key, []).append(path)

    collisions = sorted(key for key, paths in raw.items() if len(paths) > 1)
    collision_set = set(collisions)
    index = {key: paths[0] for key, paths in raw.items() if key not in collision_set}
    return index, collisions


def _index_scan_zip(
    zip_path: Path, *, resource_label: str | None
) -> tuple[dict[str, tuple[Path, str]], list[str]]:
    """Index a scan-level ZIP's members by :func:`scan_source_key`.

    Always ``wrapped=True``: every scan-level ZIP XNAT emits carries a
    session/experiment-label wrapper around its scan-tree content.
    """
    raw: dict[str, list[str]] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            key = scan_source_key(
                member.strip("/").split("/"), wrapped=True, resource_label=resource_label
            )
            if key is not None:
                raw.setdefault(key, []).append(member)

    collisions = sorted(key for key, members in raw.items() if len(members) > 1)
    collision_set = set(collisions)
    index = {
        key: (zip_path, members[0]) for key, members in raw.items() if key not in collision_set
    }
    return index, collisions


def _index_session_resource_zip(
    zip_path: Path, *, label: str, manifest: dict[str, str | None]
) -> tuple[dict[str, tuple[Path, str]], list[str]]:
    """Index a session-resource ZIP's members by :func:`session_resource_zip_member_keys`.

    Each member's candidate keys are resolved against *manifest* (ground
    truth): the first candidate the manifest actually knows about wins. When
    more than one candidate matches a manifest entry, this member's true
    name can't be told apart from the manifest alone -- reported as a
    collision on every key it ambiguously matched, rather than picked
    arbitrarily. When no candidate matches anything in the manifest (e.g. a
    genuinely extra local-only file, or a unit test manifest keyed by the
    expected shape directly), the highest-priority candidate is used, same
    as before this resolution existed.
    """
    raw: dict[str, list[str]] = {}
    ambiguous: set[str] = set()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            candidates = session_resource_zip_member_keys(member.strip("/").split("/"), label=label)
            matches = [c for c in candidates if c in manifest]
            if len(matches) > 1:
                ambiguous.update(matches)
                continue
            key = matches[0] if matches else candidates[0]
            raw.setdefault(key, []).append(member)

    collisions = sorted(ambiguous | {key for key, members in raw.items() if len(members) > 1})
    collision_set = set(collisions)
    index = {
        key: (zip_path, members[0]) for key, members in raw.items() if key not in collision_set
    }
    return index, collisions


#: A ZIP to index. A bare path is a scan-level archive (indexed with the
#: shared *resource_label*); a ``(path, label)`` pair is a session-resource
#: ZIP scoped to exactly that one label -- e.g. each
#: ``resources_{label}.zip`` is a different resource than the scan-level
#: archive/tree, and is keyed with no marker search at all (see
#: :func:`session_resource_zip_member_keys`).
ZipSource = Path | tuple[Path, str]


def verify_manifest(
    manifest: dict[str, str | None],
    *,
    local_root: Path | None = None,
    local_root_wrapped: bool = False,
    zip_paths: Sequence[ZipSource] = (),
    resource_label: str | None = None,
) -> VerificationReport:
    """Compare a server file manifest against local content.

    *local_root* (an extracted scan tree) and *zip_paths* (unextracted
    archives) are not mutually exclusive: a session download can have its
    scans extracted into *local_root* while session-level resources remain
    as separate, un-extracted ZIPs alongside it. Hashing streams the source
    -- a file or a ZIP member -- rather than reading it whole into memory,
    and runs sequentially: verification is IO-bound, and threading it here
    would add complexity the workload doesn't earn.

    Args:
        manifest: Server-reported ``key -> digest`` map (see
            :func:`key_from_uri`); a None digest marks a file the server
            listed with no checksum on record.
        local_root: Root of an extracted scan tree.
        local_root_wrapped: Whether *local_root*'s own tree carries a
            session/experiment-label wrapper -- see :func:`scan_source_key`.
            The caller already knows this from how it produced *local_root*
            (e.g. ``scan download --extract``'s tree is wrapped; a session
            download's own extracted tree is not).
        zip_paths: Unextracted archive(s) to read members from -- see
            :data:`ZipSource`.
        resource_label: Passed through to :func:`scan_source_key` for the
            pre-scoped single-resource ZIP fallback.

    Returns:
        A :class:`~xnatctl.models.progress.VerificationReport` covering every
        manifest entry, plus any local file the manifest never mentioned
        (``missing_remote``) and any key two unrelated files both mapped to
        (``collisions``) -- including across two different ZIPs, or one
        local file and one ZIP member.
    """
    local_index: dict[str, Path] = {}
    collisions: set[str] = set()
    if local_root is not None:
        local_index, local_collisions = _index_local_tree(
            local_root, resource_label=resource_label, wrapped=local_root_wrapped
        )
        collisions.update(local_collisions)
        local_index = _fold_local_dups(local_index, manifest, collisions)

    zip_raw: dict[str, list[tuple[Path, str]]] = {}
    for source in zip_paths:
        if isinstance(source, tuple):
            zip_path, label = source
            zi, zc = _index_session_resource_zip(zip_path, label=label, manifest=manifest)
        else:
            zi, zc = _index_scan_zip(source, resource_label=resource_label)
        collisions.update(zc)
        for key, value in zi.items():
            zip_raw.setdefault(key, []).append(value)

    cross_zip_collisions = {key for key, values in zip_raw.items() if len(values) > 1}
    collisions.update(cross_zip_collisions)
    zip_index = {key: values[0] for key, values in zip_raw.items() if len(values) == 1}

    cross_source_collisions = set(local_index) & set(zip_index)
    for key in cross_source_collisions:
        local_index.pop(key, None)
        zip_index.pop(key, None)
    collisions.update(cross_source_collisions)

    report = VerificationReport(collisions=sorted(collisions))
    open_zips: dict[Path, zipfile.ZipFile] = {}
    try:
        for key, digest in manifest.items():
            if key in collisions:
                continue
            if key in local_index:
                actual = _md5_file(local_index[key])
            elif key in zip_index:
                zpath, member = zip_index[key]
                zf = open_zips.get(zpath)
                if zf is None:
                    zf = zipfile.ZipFile(zpath, "r")
                    open_zips[zpath] = zf
                actual = _md5_zip_member(zf, member)
            else:
                report.missing_local.append(key)
                continue

            if not digest:
                report.unverifiable.append(key)
            elif actual.lower() == digest.lower():
                report.matched += 1
            else:
                report.mismatched.append(key)
    finally:
        for zf in open_zips.values():
            zf.close()

    seen = manifest.keys()
    report.missing_remote.extend(
        key for key in local_index if key not in seen and key not in collisions
    )
    report.missing_remote.extend(
        key for key in zip_index if key not in seen and key not in collisions
    )

    return report
