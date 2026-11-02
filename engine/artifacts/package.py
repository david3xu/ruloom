"""Release packaging: manifest-driven, frozen, atomic, reproducible.

The release member list comes from the profile's declared ``release``
block, never from a hard-coded file list. Members whose ``when``
condition is unmet (for example image evidence when the imaging branch
did not run) are absent; every other required member must be present
or packaging refuses loudly.

Packaging reads only the frozen snapshot the engine took when the run
reached the human gate, plus the engine-serialised run log -- never
mutable working files. A working file mutated or deleted during the
review prompt cannot change the released bytes. The ZIP is built in a
temporary file and renamed into place, so a failure mid-packaging
leaves no partial ``release.zip``; the same frozen inputs always yield
the same ZIP bytes (fixed member timestamps).
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .hashing import sha256_bytes, sha256_canonical

#: Fixed DOS timestamp for reproducible ZIP members (year, mon, day, h, m, s).
ZIP_FIXED_DATE = (1980, 1, 1, 0, 0, 0)

#: Fallback member list for manifests that declare no ``release`` block
#: (test-only manifests built programmatically). Shipped profiles always
#: declare their members; this fallback keeps those older shapes working.
DEFAULT_RELEASE_MEMBERS = (
    "data.csv",
    "plan.json",
    "validation-report.json",
    "seed.txt",
    "provenance.md",
    "run.jsonl",
)

#: Directory (under the run directory) holding the frozen byte copies
#: snapshotted when the run reached the human gate.
FROZEN_DIR_NAME = "frozen"

#: The frozen evidence manifest recorded before the human decision.
FROZEN_MANIFEST_NAME = "frozen-manifest.json"

#: The complete release manifest recorded alongside the release.
RELEASE_MANIFEST_NAME = "manifest.json"

#: Detached SHA-256 of ``release.zip`` (``"<sha>  release.zip"``).
RELEASE_HASH_NAME = "release.sha256"

#: Name of the reproducible bundle itself.
RELEASE_ZIP_NAME = "release.zip"

#: Name of the in-ZIP checksum file attesting every zipped member.
CHECKSUMS_NAME = "checksums.sha256"


def declared_release_members(manifest: Any) -> list[dict[str, Any]]:
    """The ordered release member specs from the profile declaration.

    Returns ``{"name", "required", ...}`` specs in manifest order. A
    manifest without a ``release`` block falls back to
    :data:`DEFAULT_RELEASE_MEMBERS` (all required, frozen-sourced).
    """
    release = getattr(manifest, "release", None)
    members: list[Any] = []
    if isinstance(release, Mapping):
        raw = release.get("members", [])
        if isinstance(raw, list):
            members = list(raw)
    if not members:
        return [{"name": name, "required": True} for name in DEFAULT_RELEASE_MEMBERS]
    return [dict(spec) for spec in members if isinstance(spec, Mapping)]


def selected_release_members(
    manifest: Any, *, images_present: bool
) -> list[dict[str, Any]]:
    """The release member specs active for this run, in manifest order.

    Members whose ``when`` condition is unmet are absent (the imaging
    branch produced nothing for this run, so its evidence stays out).
    """
    selected: list[dict[str, Any]] = []
    for spec in declared_release_members(manifest):
        condition = spec.get("when")
        if condition == "images_present" and not images_present:
            continue
        selected.append(spec)
    return selected


def release_member_names(manifest: Any, *, images_present: bool) -> list[str]:
    """The ordered release file names active for this run."""
    return [
        spec["name"]
        for spec in selected_release_members(manifest, images_present=images_present)
    ]


def frozen_member_names(manifest: Any, *, images_present: bool) -> list[str]:
    """Active members whose bytes arrive via the frozen snapshot."""
    return [
        spec["name"]
        for spec in selected_release_members(manifest, images_present=images_present)
        if spec.get("source", "frozen") == "frozen"
    ]


#: Sibling-manifest field carrying the explicit output-terms grant.
#: The verified ``*-source.json`` registry record may state, under this
#: field, the terms the synthetic output is released under (for example
#: ``"CC0-1.0"``). Licence presence alone is never such a grant: a
#: recorded source licence describes the source data, not the output.
OUTPUT_TERMS_GRANT_FIELD = "output_terms"


def resolve_output_terms(
    *,
    hash_verified: bool,
    output_terms_grant: Any,
) -> str | None:
    """Decide the release output terms from the recorded rights evidence.

    The granted term is returned only when the source bytes verified
    against the sibling manifest record at intake *and* that verified
    record carries an explicit output-terms grant under
    :data:`OUTPUT_TERMS_GRANT_FIELD`. Anything else -- no manifest, no
    recorded hash, a hash that could not verify, or a verified record
    with no explicit grant (even a permissive source licence) -- yields
    ``None``: the release then carries no output-rights clearance at
    all. There is no default dedication; callers pass the result
    through to :func:`build_provenance` explicitly.
    """
    if (
        hash_verified is True
        and isinstance(output_terms_grant, str)
        and output_terms_grant.strip()
    ):
        return output_terms_grant.strip()
    return None


def rights_block(
    *,
    source_sha256: str,
    licence: Any,
    attribution: Any,
    manifest_name: Any,
    manifest_sha256: Any,
    hash_verified: Any,
    output_terms: str | None,
    output_terms_grant: Any = None,
) -> dict[str, Any]:
    """The release-manifest rights evidence for one run.

    Source identity, licence/attribution, and verification outcome are
    recorded as separate fields -- never folded into a single claim -- so
    a recipient can re-derive the output-terms decision. ``output_terms``
    is the :func:`resolve_output_terms` result, or ``None`` when the
    release carries no output-rights clearance. The grant evidence itself
    is recorded alongside as the field name
    (:data:`OUTPUT_TERMS_GRANT_FIELD`) plus the recorded value, even when
    the source is unverified and the grant therefore clears nothing.
    """
    verified = hash_verified is True
    grant = (
        output_terms_grant.strip()
        if isinstance(output_terms_grant, str) and output_terms_grant.strip()
        else None
    )
    return {
        "source_sha256": source_sha256,
        "source_licence": licence,
        "source_attribution": attribution,
        "manifest_name": manifest_name,
        "manifest_sha256": manifest_sha256,
        "source_hash_verified": verified,
        "output_terms": output_terms if verified else None,
        "output_terms_grant_field": (
            OUTPUT_TERMS_GRANT_FIELD if grant is not None else None
        ),
        "output_terms_grant": grant,
    }


def build_provenance(
    *,
    source_name: str,
    source_sha256: str,
    licence: str | None,
    attribution: str | None,
    artifact_sha256: str,
    plan: Mapping[str, Any],
    verdict: str,
    run_id: str,
    output_terms: str | None = None,
    hash_verified: bool = False,
    manifest_name: str | None = None,
) -> str:
    """Provenance statement for one released artifact.

    ``output_terms`` carries the :func:`resolve_output_terms` decision:
    only an explicit, evidence-backed value produces a public-dedication
    sentence. ``None`` (the default) renders an explicit no-clearance
    statement instead -- no dedication is ever implied. ``hash_verified``
    and ``manifest_name`` render the verification outcome, so the reader
    sees *why* the output terms read as they do.
    """
    lines = [
        "# Provenance",
        "",
        f"Run: {run_id}",
        f"Source: {source_name} (sha256 {source_sha256})",
    ]
    if licence:
        lines.append(f"Source licence: {licence}")
    if attribution:
        lines.append(f"Source attribution: {attribution}")
    if hash_verified and manifest_name:
        lines.append(
            "Source verification: source hash verified against sibling "
            f"manifest {manifest_name} at intake."
        )
    else:
        lines.append(
            "Source verification: UNVERIFIED at intake (no matching sibling "
            "manifest record); licence and attribution above are "
            "unconfirmed."
        )
    lines += [
        f"Artifact sha256: {artifact_sha256}",
        f"Plan: seed={plan['seed']} n_rows={plan['n_rows']}",
        f"Validation verdict: {verdict}",
        "",
    ]
    if output_terms:
        lines += [
            "Only synthetic output generated by Ruloom -- never the source rows",
            f"-- is released under {output_terms} (public domain dedication).",
        ]
    else:
        lines += [
            "Output rights: NOT CLEARED -- this release carries no",
            "public-dedication claim. Clear rights separately before",
            "redistributing; the unverified source terms above are not",
            "a grant.",
        ]
    lines += [
        "Source data terms above continue to apply to the source data.",
        "Rule 4: a human accepted this release; no model accepts its own output.",
        "",
    ]
    return "\n".join(lines)


def candidate_entries(
    files: Mapping[str, bytes], *, attempt: int
) -> list[dict[str, Any]]:
    """Manifest entries for frozen bytes: path, size, sha, producer."""
    return [
        {
            "path": name,
            "size": len(payload),
            "sha256": sha256_bytes(bytes(payload)),
            "attempt": attempt,
        }
        for name, payload in files.items()
    ]


def candidate_digest(entries: list[dict[str, Any]]) -> str:
    """The digest the human decision binds: canonical hash of entries."""
    return sha256_canonical(entries)


def _atomic_write(path: Path, payload: bytes) -> Path:
    """Write ``payload`` to ``path`` atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".tmp."
    )
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def write_frozen_snapshot(run_dir: Path, files: Mapping[str, bytes]) -> Path:
    """Persist frozen byte copies under ``run_dir/frozen/`` (evidence)."""
    frozen_dir = Path(run_dir) / FROZEN_DIR_NAME
    frozen_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (frozen_dir / name).write_bytes(bytes(payload))
    return frozen_dir


def write_frozen_manifest(run_dir: Path, payload: Mapping[str, Any]) -> Path:
    """Record the candidate manifest snapshotted before the decision."""
    path = Path(run_dir, FROZEN_MANIFEST_NAME)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_release_manifest(run_dir: Path, payload: Mapping[str, Any]) -> Path:
    """Record the complete release manifest alongside the release."""
    return _atomic_write(
        Path(run_dir, RELEASE_MANIFEST_NAME),
        (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def write_detached_hash(run_dir: Path, zip_digest: str) -> Path:
    """Write the detached ``release.sha256`` hash of ``release.zip``."""
    return _atomic_write(
        Path(run_dir, RELEASE_HASH_NAME),
        f"{zip_digest}  {RELEASE_ZIP_NAME}\n".encode(),
    )


def build_checksums(files: Mapping[str, bytes]) -> str:
    """The ``checksums.sha256`` text attesting every member's bytes."""
    return "".join(
        f"{sha256_bytes(bytes(payload))}  {name}\n"
        for name, payload in sorted(files.items())
    )


def build_zip_bytes(files: Mapping[str, bytes]) -> bytes:
    """Deterministic ZIP bytes for one ordered member mapping.

    Identical frozen inputs always yield identical bytes: member
    timestamps, permissions, order (sorted by name), and compression
    are all fixed.
    """
    import io as _io

    buffer = _io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, bytes(files[name]))
    return buffer.getvalue()


def write_release_zip(
    artifact_dir: Path, files: Mapping[str, bytes]
) -> tuple[Path, str]:
    """Write the reproducible ``release.zip`` from frozen bytes, atomically.

    ``files`` maps member name to the exact bytes to ship (the frozen
    snapshot plus engine-serialised log, never working files). The ZIP
    is built in a temporary file, verified by the consumer check, and
    renamed into place; any failure leaves no partial ``release.zip``.
    The checksum text is also kept as the loose ``checksums.sha256``
    file beside the bundle. Returns the ZIP path and its SHA-256.
    """
    from .consumer import verify_release as _verify

    ordered = {name: bytes(files[name]) for name in sorted(files)}
    checksums_text = build_checksums(ordered)
    payload = ordered | {CHECKSUMS_NAME: checksums_text.encode("utf-8")}
    zip_bytes = build_zip_bytes(payload)
    zip_digest = sha256_bytes(zip_bytes)
    run_dir = Path(artifact_dir)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(run_dir), prefix=RELEASE_ZIP_NAME + ".tmp."
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(zip_bytes)
        _verify(tmp_path)
        os.replace(tmp_name, run_dir / RELEASE_ZIP_NAME)
        _atomic_write(run_dir / CHECKSUMS_NAME, checksums_text.encode("utf-8"))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        for leftover in (run_dir / RELEASE_ZIP_NAME, run_dir / CHECKSUMS_NAME):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    return run_dir / RELEASE_ZIP_NAME, zip_digest
