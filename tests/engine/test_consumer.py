"""Consumer-side verification of a release bundle.

The recipient holding only ``release.zip`` re-establishes every
producer guarantee: safe entry names, complete membership, checksums,
readable JSON, required report fields, and report-to-payload reference
binding. Each tamper shape below must fail loudly.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from engine.artifacts.consumer import ReleaseVerificationError, verify_release
from engine.artifacts.package import (
    CHECKSUMS_NAME,
    build_checksums,
    build_zip_bytes,
    write_release_zip,
)


def _payload() -> bytes:
    return b"value,label\n1,A\n2,B\n"


def _report(payload: bytes) -> dict[str, object]:
    return {
        "artifact_name": "consumer-fixture:attempt-1",
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "verdict": "pass",
        "results": [],
    }


def _files(payload: bytes | None = None) -> dict[str, bytes]:
    data = _payload() if payload is None else payload
    return {
        "data.csv": data,
        "plan.json": json.dumps(
            {"seed": 1, "n_rows": 2}, indent=2, sort_keys=True
        ).encode()
        + b"\n",
        "validation-report.json": json.dumps(
            _report(data), indent=2, sort_keys=True
        ).encode()
        + b"\n",
        "seed.txt": b"1\n",
        "provenance.md": b"# Provenance\n",
        "run.jsonl": b'{"stage": "run_start", "status": "ok"}\n',
    }


def _seal(tmp_path: Path, files: dict[str, bytes], name: str) -> Path:
    """Write an explicit ZIP (bypasses the producer self-check)."""
    ordered = {key: files[key] for key in sorted(files)}
    payload = ordered | {CHECKSUMS_NAME: build_checksums(ordered).encode("utf-8")}
    path = tmp_path / name
    path.write_bytes(build_zip_bytes(payload))
    return path


def _seal_raw(tmp_path: Path, entries: dict[str, bytes], name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(build_zip_bytes(entries))
    return path


def _valid_release(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    zip_path, _ = write_release_zip(out, _files())
    return zip_path


def test_valid_release_verifies(tmp_path: Path) -> None:
    zip_path = _valid_release(tmp_path)
    outcome = verify_release(zip_path, manifest=list(_files()))
    assert outcome["artifact_sha256"] == hashlib.sha256(_payload()).hexdigest()
    assert outcome["members"] == sorted(_files())
    assert outcome["zip_sha256"] == hashlib.sha256(zip_path.read_bytes()).hexdigest()


def test_byte_flip_fails(tmp_path: Path) -> None:
    files = _files()
    stale = build_checksums(files)
    files["data.csv"] = b"value,label\n1,TAMPERED\n"
    # Attacker reuses the original checksums: the hash no longer matches.
    path = _seal_raw(tmp_path, files | {CHECKSUMS_NAME: stale.encode()}, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_bit_flip_inside_compressed_data_is_a_refusal(tmp_path: Path) -> None:
    # Corruption inside the deflated bytes must surface as a verification
    # refusal, never as a raw decoding crash (zlib.error/BadZipFile).
    zip_path = _valid_release(tmp_path)
    raw = bytearray(zip_path.read_bytes())
    raw[100] ^= 0xFF
    broken = tmp_path / "broken.zip"
    broken.write_bytes(bytes(raw))
    with pytest.raises(ReleaseVerificationError):
        verify_release(broken)


def test_missing_member_fails(tmp_path: Path) -> None:
    files = _files()
    remaining = {k: v for k, v in files.items() if k != "seed.txt"}
    ordered = {key: remaining[key] for key in sorted(remaining)}
    # The checksums still attest seed.txt, but the entry is gone.
    payload = ordered | {CHECKSUMS_NAME: build_checksums(files).encode("utf-8")}
    path = tmp_path / "t.zip"
    path.write_bytes(build_zip_bytes(payload))
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_missing_member_with_recut_checksums_fails_manifest_check(
    tmp_path: Path,
) -> None:
    """A member dropped together with its checksum line still fails.

    Against the declared manifest the bundle is short, even though the
    remaining checksums are self-consistent.
    """
    remaining = {k: v for k, v in _files().items() if k != "seed.txt"}
    ordered = {key: remaining[key] for key in sorted(remaining)}
    payload = ordered | {CHECKSUMS_NAME: build_checksums(ordered).encode("utf-8")}
    path = tmp_path / "t.zip"
    path.write_bytes(build_zip_bytes(payload))
    with pytest.raises(ReleaseVerificationError):
        verify_release(path, manifest=list(_files()))


def test_substituted_older_report_fails(tmp_path: Path) -> None:
    """An older report with re-cut checksums still fails the binding.

    The checksums match the substituted bytes, but the older report
    binds a different artifact hash than the shipped payload.
    """
    files = _files()
    older = dict(_report(b"older-bytes"))
    files["validation-report.json"] = (
        json.dumps(older, indent=2, sort_keys=True).encode() + b"\n"
    )
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_traversal_entry_fails(tmp_path: Path) -> None:
    files = _files()
    files["../evil.txt"] = b"escape"
    path = _seal_raw(
        tmp_path,
        files | {CHECKSUMS_NAME: build_checksums(files).encode("utf-8")},
        "t.zip",
    )
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_absolute_entry_fails(tmp_path: Path) -> None:
    files = _files()
    path = tmp_path / "t.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("/abs.txt", b"absolute")
        archive.writestr(CHECKSUMS_NAME, build_checksums(files).encode("utf-8"))
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_invalid_json_member_fails(tmp_path: Path) -> None:
    files = _files()
    files["plan.json"] = b"{not json"
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_report_missing_required_field_fails(tmp_path: Path) -> None:
    files = _files()
    thin = dict(_report(files["data.csv"]))
    del thin["artifact_sha256"]
    files["validation-report.json"] = (
        json.dumps(thin, indent=2, sort_keys=True).encode() + b"\n"
    )
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_undeclared_extra_member_fails(tmp_path: Path) -> None:
    files = _files()
    files["extra.txt"] = b"rides along"
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path, manifest=list(_files()))


def _manifest_with_hashes(files: dict[str, bytes]) -> dict[str, object]:
    return {
        "members": [
            {
                "path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(files.items())
        ]
    }


def test_stale_manifest_hash_refuses_altered_member(tmp_path: Path) -> None:
    """A changed report with re-cut internal checksums still fails.

    The ZIP is self-consistent, but the external manifest still binds
    the original bytes: the consumer must verify recorded hashes, not
    only member names.
    """
    files = _files()
    manifest = _manifest_with_hashes(files)
    report = _report(files["data.csv"])
    report["verdict"] = "fail"
    report["results"] = [
        {
            "name": "audit_counterexample",
            "status": "fail",
            "detail": "changed after acceptance",
        }
    ]
    files["validation-report.json"] = (
        json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    )
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path, manifest=manifest)


def test_manifest_size_mismatch_refuses(tmp_path: Path) -> None:
    files = _files()
    manifest = _manifest_with_hashes(files)
    members = manifest["members"]
    assert isinstance(members, list)
    entry = dict(members[0])
    entry["size"] = int(entry["size"]) + 1
    manifest["members"] = [entry] + list(members[1:])
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path, manifest=manifest)


def test_non_pass_verdict_refuses(tmp_path: Path) -> None:
    """A report verdict must read exactly ``pass``: fail is a refusal."""
    files = _files()
    report = _report(files["data.csv"])
    report["verdict"] = "fail"
    report["results"] = [
        {
            "name": "audit_counterexample",
            "status": "fail",
            "detail": "changed after acceptance",
        }
    ]
    files["validation-report.json"] = (
        json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    )
    path = _seal(tmp_path, files, "t.zip")
    with pytest.raises(ReleaseVerificationError):
        verify_release(path, manifest=list(files))
