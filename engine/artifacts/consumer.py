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
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

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

#: Exact declared members for a case release bundle.
CASE_MEMBER_NAMES = frozenset(
    {
        "case-binding.json",
        "case-contract.json",
        "case-reference.json",
        "case-utility.json",
        "case-evidence.json",
    }
)


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
        raise ReleaseVerificationError(f"release entry {name!r} is absolute: refusing")
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
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
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


def _expected_members(
    manifest: Any,
) -> tuple[set[str], dict[str, dict[str, Any]]] | None:
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


def _check_report(member: str, payload: Any, *, members: dict[str, bytes]) -> str:
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
        raise ReleaseVerificationError(f"{member} carries no artifact hash: refusing")
    if payload["verdict"] != "pass":
        raise ReleaseVerificationError(
            f"{member} verdict is {payload['verdict']!r}, not 'pass': refusing"
        )
    if not isinstance(payload["results"], list):
        raise ReleaseVerificationError(f"{member} carries no results list: refusing")
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
    zip_path: str | Path,
    *,
    manifest: Any = None,
    case_verifier: Any = None,
    expected_case: Mapping[str, Any] | None = None,
    source_path: Path | str | None = None,
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
            if name.startswith("case-") and name not in CASE_MEMBER_NAMES:
                raise ReleaseVerificationError(
                    f"release bundle carries unexpected case member {name!r}: refusing"
                )
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
                    f"attested member {name!r} is missing from the bundle: refusing"
                )
        declared, recorded = _expected_members(manifest)
        if declared is not None and recorded is not None:
            for name in sorted(declared):
                if name not in names:
                    raise ReleaseVerificationError(
                        f"declared member {name!r} is missing from the bundle: refusing"
                    )
            for name in names:
                if name != CHECKSUMS_NAME and name not in declared:
                    raise ReleaseVerificationError(
                        f"bundle member {name!r} is not in the declared "
                        "manifest: refusing"
                    )
        with tempfile.TemporaryDirectory(prefix="ruloom-release-") as staging:
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
                f"member {name!r} does not match its recorded checksum: refusing"
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
        raise ReleaseVerificationError(f"bundle carries no {REPORT_MEMBER}: refusing")
    artifact_sha = _check_report(REPORT_MEMBER, parsed[REPORT_MEMBER], members=members)
    if IMAGE_REPORT_MEMBER in parsed:
        _check_report(IMAGE_REPORT_MEMBER, parsed[IMAGE_REPORT_MEMBER], members=members)

    case_report: dict[str, Any] | None = None
    case_members_present = {name for name in members if name in CASE_MEMBER_NAMES}
    has_case_members = bool(case_members_present)
    if has_case_members:
        missing_case = sorted(CASE_MEMBER_NAMES - case_members_present)
        if missing_case:
            raise ReleaseVerificationError(
                f"release bundle is missing declared case members {missing_case}: refusing"
            )
        if PAYLOAD_MEMBER not in members:
            raise ReleaseVerificationError(
                f"case release bundle is missing payload {PAYLOAD_MEMBER!r}: refusing"
            )
    if expected_case is not None and not has_case_members:
        raise ReleaseVerificationError(
            f"expected case {expected_case!r} was required, but release bundle "
            f"contains no case-binding.json: refusing (strip attack detected)"
        )
    if has_case_members:
        import importlib

        import profiles as profiles_package

        target_profile: str | None = None
        if manifest is not None and isinstance(manifest, Mapping):
            target_profile = manifest.get("profile_id") or manifest.get("profile")
        if target_profile is None:
            binding_raw = members.get("case-binding.json", b"")
            try:
                binding_obj = json.loads(binding_raw.decode("utf-8"))
                cand_case_id = (
                    binding_obj.get("case_id")
                    if isinstance(binding_obj, Mapping)
                    else None
                )
            except Exception:
                cand_case_id = None
            if isinstance(cand_case_id, str):
                for pid in profiles_package.available_profiles():
                    p_manifest = profiles_package.manifest_dict(pid)
                    cases_list = p_manifest.get("cases") or p_manifest.get(
                        "generation", {}
                    ).get("cases", [])
                    if isinstance(cases_list, list):
                        for c_entry in cases_list:
                            if (
                                isinstance(c_entry, Mapping)
                                and c_entry.get("id") == cand_case_id
                            ):
                                target_profile = pid
                                break
                    if target_profile is not None:
                        break

        verifier = case_verifier
        check_converter = None
        if target_profile is not None:
            try:
                profiles_package.profile_dir(target_profile)
                mod = importlib.import_module(f"profiles.{target_profile}.capabilities")
                if verifier is None:
                    verifier = getattr(mod, "verify_case_members", None)
                check_converter = getattr(mod, "case_check_results", None)
            except Exception as exc:
                if verifier is None:
                    raise ReleaseVerificationError(
                        f"failed to load verifier for profile {target_profile!r}: {exc}"
                    ) from exc

        if verifier is None:
            raise ReleaseVerificationError(
                "release bundle contains case members but no trusted verifier could be resolved"
            )

        if check_converter is None:
            check_converter = getattr(verifier, "case_check_results", None)
            if check_converter is None and hasattr(verifier, "__globals__"):
                check_converter = verifier.__globals__.get("case_check_results")

        if check_converter is None:
            raise ReleaseVerificationError(
                "release bundle contains case members but no trusted case check converter could be resolved"
            )

        case_subset = {
            name: members[name] for name in sorted(CASE_MEMBER_NAMES | {PAYLOAD_MEMBER})
        }
        case_result = verifier(
            case_subset, expected_case=expected_case, source_path=source_path
        )
        if not isinstance(case_result, Mapping):
            raise ReleaseVerificationError(
                f"case verifier returned invalid type {type(case_result).__name__}"
            )
        if not case_result.get("verified"):
            discrepancies = case_result.get("discrepancies", [])
            raise ReleaseVerificationError(f"case verification failed: {discrepancies}")

        # Strict validation of validation-report.json case results and case_utility
        util_verif = case_result.get("utility_verification")
        if not isinstance(util_verif, Mapping):
            raise ReleaseVerificationError(
                "case verifier result missing utility_verification mapping: refusing"
            )
        recomputed = util_verif.get("recomputed")
        if not isinstance(recomputed, Mapping):
            raise ReleaseVerificationError(
                "case verifier result missing recomputed utility mapping: refusing"
            )

        expected_results = check_converter(recomputed)
        if not isinstance(expected_results, list) or len(expected_results) == 0:
            raise ReleaseVerificationError(
                "trusted case check converter produced no expected results: refusing"
            )
        expected_by_id = {
            cr["check_id"]: cr
            for cr in expected_results
            if isinstance(cr, Mapping) and "check_id" in cr
        }

        report_payload = parsed.get(REPORT_MEMBER)
        if not isinstance(report_payload, Mapping):
            raise ReleaseVerificationError(
                f"{REPORT_MEMBER} is missing or not a JSON object: refusing"
            )

        report_case_utility = report_payload.get("case_utility")
        if not isinstance(report_case_utility, Mapping):
            raise ReleaseVerificationError(
                f"{REPORT_MEMBER} is missing required 'case_utility' mapping: refusing"
            )
        if report_case_utility.get("passed") is not True:
            raise ReleaseVerificationError(
                f"{REPORT_MEMBER} 'case_utility' passed is not True: refusing"
            )
        shipped_case_utility = parsed.get("case-utility.json")
        if not isinstance(shipped_case_utility, Mapping):
            raise ReleaseVerificationError(
                "release bundle carries missing or invalid case-utility.json: refusing"
            )
        if report_case_utility != shipped_case_utility:
            raise ReleaseVerificationError(
                f"{REPORT_MEMBER} 'case_utility' does not match shipped case-utility.json: refusing"
            )

        report_results = report_payload.get("results")
        if not isinstance(report_results, list):
            raise ReleaseVerificationError(
                f"{REPORT_MEMBER} results is not a list: refusing"
            )

        report_case_entries: dict[str, list[dict[str, Any]]] = {}
        for r in report_results:
            if not isinstance(r, Mapping):
                continue
            r_name = r.get("name")
            if isinstance(r_name, str) and (
                r_name in expected_by_id or r_name.startswith("case.")
            ):
                report_case_entries.setdefault(r_name, []).append(r)

        for check_id, exp_cr in expected_by_id.items():
            entries = report_case_entries.get(check_id)
            if not entries:
                raise ReleaseVerificationError(
                    f"{REPORT_MEMBER} missing required case check {check_id!r}: refusing"
                )
            if len(entries) != 1:
                raise ReleaseVerificationError(
                    f"{REPORT_MEMBER} has duplicate case check {check_id!r}: refusing"
                )
            actual = entries[0]
            if actual.get("status") != "pass":
                raise ReleaseVerificationError(
                    f"{REPORT_MEMBER} case check {check_id!r} has status "
                    f"{actual.get('status')!r}, expected 'pass': refusing"
                )
            if str(actual.get("detail", "")) != str(exp_cr.get("detail", "")):
                raise ReleaseVerificationError(
                    f"{REPORT_MEMBER} case check {check_id!r} detail does not match "
                    "recomputed utility check detail: refusing"
                )

        extra_case_checks = set(report_case_entries.keys()) - set(expected_by_id.keys())
        if extra_case_checks:
            raise ReleaseVerificationError(
                f"{REPORT_MEMBER} carries unexpected case checks "
                f"{sorted(extra_case_checks)}: refusing"
            )

        case_report = dict(case_result)
    else:
        case_report = {
            "verified": False,
            "status": "not case-checked",
            "note": "release bundle contains no case members",
        }

    out: dict[str, Any] = {
        "members": sorted(members),
        "artifact_sha256": artifact_sha,
        "zip_sha256": sha256_bytes(archive_path.read_bytes()),
    }
    if case_report is not None:
        out["case"] = case_report
    return out
