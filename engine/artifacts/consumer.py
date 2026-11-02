"""Consumer-side verification of a release bundle.

A recipient holding only ``release.zip`` (plus, when available, the
run's ``manifest.json``) can re-establish every guarantee the producer
claims, without trusting the producer's working files:

* entry hygiene -- no absolute paths and no parent-directory escapes,
  so extraction cannot write outside the destination;
* completeness -- every declared member is present and no unexpected
  entry rides along; when the manifest records hashes and sizes, the
  shipped bytes must match those too, not only the member names;
* integrity -- every member's bytes hash to the ``checksums.sha256``
  value recorded alongside them;
* readability -- every ``.json`` member parses;
* report contract -- the validation report carries its required fields
  and its verdict reads exactly ``pass``;
* reference binding -- the report's artifact hash resolves to the
  shipped payload bytes (a substituted older report fails even when
  the checksums were re-cut around it).

Any violation raises :class:`ReleaseVerificationError` loudly; a
return (never ``None``) means every check passed.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .hashing import sha256_bytes

#: Members the consumer always expects inside the bundle (besides the
#: checksum file itself, which attests the rest but not itself).
CHECKSUMS_NAME = "checksums.sha256"

#: The shipped payload the validation report must bind.
PAYLOAD_MEMBER = "data.csv"

#: The validation report carrying the verdict and the artifact binding.
REPORT_MEMBER = "validation-report.json"

#: The image evidence members, present only when the imaging branch ran.
IMAGE_MEMBER = "synthetic.png"
IMAGE_REPORT_MEMBER = "image-validation.json"

#: Required fields of a validation report (each with its expected shape).
REPORT_REQUIRED_FIELDS = ("artifact_name", "artifact_sha256", "verdict", "results")


class ReleaseVerificationError(Exception):
    """A release bundle failed consumer-side verification."""


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one bundle member; undecodable bytes are a refusal, not a crash."""
    try:
        return archive.read(name)
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        raise ReleaseVerificationError(
            f"release member {name!r} cannot be decoded: refusing ({exc})"
        ) from exc


def _check_entry_name(name: str) -> None:
    """Reject absolute paths, escapes, and non-flat entries."""
    if not name:
        raise ReleaseVerificationError("release holds an empty entry name")
    if "\\" in name:
        raise ReleaseVerificationError(
            f"release entry {name!r} carries a backslash: refusing"
        )
    parsed = PurePosixPath(name)
    if parsed.is_absolute():
        raise ReleaseVerificationError(
            f"release entry {name!r} is absolute: refusing"
        )
    if ".." in parsed.parts:
        raise ReleaseVerificationError(
            f"release entry {name!r} escapes its directory: refusing"
        )
    if len(parsed.parts) != 1:
        raise ReleaseVerificationError(
            f"release entry {name!r} is not a flat file name: refusing"
        )


def _parse_checksums(text: str) -> dict[str, str]:
    """Parse ``checksums.sha256`` lines into ``{name: sha}`` (strict)."""
    expected: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("  ")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ReleaseVerificationError(
                f"checksums.sha256 line {lineno} is malformed: refusing"
            )
        digest, name = parts
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ReleaseVerificationError(
                f"checksums.sha256 line {lineno} carries a bad digest: refusing"
            )
        if name in expected:
            raise ReleaseVerificationError(
                f"checksums.sha256 lists {name!r} twice: refusing"
            )
        expected[name] = digest
    if not expected:
        raise ReleaseVerificationError("checksums.sha256 lists no members")
    return expected


def _expected_members(manifest: Any) -> tuple[set[str], dict[str, dict[str, Any]]] | None:
    """Normalise the manifest argument to declared members, if any.

    Returns the declared names plus, per name, the recorded ``sha256``
    and ``size`` when the manifest carries them (a plain sequence of
    names declares no hashes or sizes). Recorded values are returned
    verbatim; :func:`verify_release` refuses on any mismatch.
    """
    if manifest is None:
        return set(), None
    if isinstance(manifest, Mapping):
        members = manifest.get("members", [])
        names: set[str] = set()
        recorded: dict[str, dict[str, Any]] = {}
        if isinstance(members, list):
            for entry in members:
                if isinstance(entry, Mapping):
                    name = entry.get("path", entry.get("name"))
                    if isinstance(name, str) and name:
                        names.add(name)
                        recorded[name] = {
                            "sha256": entry.get("sha256"),
                            "size": entry.get("size"),
                        }
        return names, recorded
    if isinstance(manifest, Sequence) and not isinstance(manifest, (str, bytes)):
        return {str(name) for name in manifest}, {}
    raise ReleaseVerificationError("manifest must list member names")


def _check_report(
    member: str, payload: Any, *, members: dict[str, bytes]
) -> str:
    """Validate one report member; return its bound artifact hash."""
    if not isinstance(payload, Mapping):
        raise ReleaseVerificationError(
            f"{member} must decode to a JSON object: refusing"
        )
    for field in REPORT_REQUIRED_FIELDS:
        if field not in payload:
            raise ReleaseVerificationError(
                f"{member} is missing required field {field!r}: refusing"
            )
    bound = payload["artifact_sha256"]
    if not isinstance(bound, str) or not bound:
        raise ReleaseVerificationError(
            f"{member} carries no artifact hash: refusing"
        )
    if payload["verdict"] != "pass":
        raise ReleaseVerificationError(
            f"{member} verdict is {payload['verdict']!r}, not 'pass': "
            "refusing"
        )
    if not isinstance(payload["results"], list):
        raise ReleaseVerificationError(
            f"{member} carries no results list: refusing"
        )
    target = PAYLOAD_MEMBER if member == REPORT_MEMBER else IMAGE_MEMBER
    try:
        current = members[target]
    except KeyError as exc:
        raise ReleaseVerificationError(
            f"{member} binds {target!r}, which is absent: refusing"
        ) from exc
    if sha256_bytes(bytes(current)) != bound:
        raise ReleaseVerificationError(
            f"{member} binds artifact {bound[:16]}..., but the shipped "
            f"{target} hashes differently: refusing"
        )
    return bound


def verify_release(
    zip_path: str | Path, *, manifest: Any = None
) -> dict[str, Any]:
    """Verify a release bundle; return its attested identity on success.

    ``manifest`` optionally carries the declared member list (the run's
    ``manifest.json`` payload or a plain sequence of names): every
    declared member must be present and no undeclared entry may ride
    along. When manifest entries record ``sha256`` and ``size``, the
    shipped bytes must match those too -- a re-cut internal checksum
    around substituted bytes still fails against the stale external
    record. Without a manifest, completeness is checked against
    ``checksums.sha256`` alone.
    """
    archive_path = Path(zip_path)
    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise ReleaseVerificationError(
            f"{archive_path} is not a readable ZIP: refusing"
        ) from exc
    with archive:
        names = archive.namelist()
        for name in names:
            _check_entry_name(name)
        if CHECKSUMS_NAME not in names:
            raise ReleaseVerificationError(
                f"{archive_path} carries no {CHECKSUMS_NAME}: refusing"
            )
        try:
            checksums_raw = _read_member(archive, CHECKSUMS_NAME).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseVerificationError(
                f"{CHECKSUMS_NAME} is not valid UTF-8: refusing"
            ) from exc
        expected = _parse_checksums(checksums_raw)
        for name in names:
            if name == CHECKSUMS_NAME:
                continue
            if name not in expected:
                raise ReleaseVerificationError(
                    f"release member {name!r} is not attested by "
                    f"{CHECKSUMS_NAME}: refusing"
                )
        for name in expected:
            if name not in names:
                raise ReleaseVerificationError(
                    f"attested member {name!r} is missing from the "
                    "bundle: refusing"
                )
        declared, recorded = _expected_members(manifest)
        if declared is not None and recorded is not None:
            for name in sorted(declared):
                if name not in names:
                    raise ReleaseVerificationError(
                        f"declared member {name!r} is missing from the "
                        "bundle: refusing"
                    )
            for name in names:
                if name != CHECKSUMS_NAME and name not in declared:
                    raise ReleaseVerificationError(
                        f"bundle member {name!r} is not in the declared "
                        "manifest: refusing"
                    )
        with tempfile.TemporaryDirectory(
            prefix="ruloom-release-"
        ) as staging:
            stage = Path(staging)
            try:
                archive.extractall(stage)
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
                raise ReleaseVerificationError(
                    f"{archive_path} cannot be extracted: refusing ({exc})"
                ) from exc
            members: dict[str, bytes] = {}
            for name in names:
                if name == CHECKSUMS_NAME:
                    continue
                members[name] = (stage / name).read_bytes()
    for name, digest in expected.items():
        if sha256_bytes(members[name]) != digest:
            raise ReleaseVerificationError(
                f"member {name!r} does not match its recorded "
                "checksum: refusing"
            )
    if manifest is not None:
        _, recorded = _expected_members(manifest)
        if recorded:
            for name in sorted(recorded):
                if name not in members:
                    continue
                entry = recorded[name]
                want_sha = entry.get("sha256")
                if want_sha is not None and sha256_bytes(members[name]) != want_sha:
                    raise ReleaseVerificationError(
                        f"member {name!r} does not match its manifest "
                        "recorded hash: refusing"
                    )
                want_size = entry.get("size")
                if want_size is not None and len(members[name]) != want_size:
                    raise ReleaseVerificationError(
                        f"member {name!r} does not match its manifest "
                        "recorded size: refusing"
                    )
    parsed: dict[str, Any] = {}
    for name, payload in members.items():
        if not name.endswith(".json"):
            continue
        try:
            parsed[name] = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseVerificationError(
                f"member {name!r} is not valid JSON: refusing"
            ) from exc
    if REPORT_MEMBER not in parsed:
        raise ReleaseVerificationError(
            f"bundle carries no {REPORT_MEMBER}: refusing"
        )
    artifact_sha = _check_report(
        REPORT_MEMBER, parsed[REPORT_MEMBER], members=members
    )
    if IMAGE_REPORT_MEMBER in parsed:
        _check_report(
            IMAGE_REPORT_MEMBER, parsed[IMAGE_REPORT_MEMBER], members=members
        )
    return {
        "members": sorted(members),
        "artifact_sha256": artifact_sha,
        "zip_sha256": sha256_bytes(archive_path.read_bytes()),
    }
