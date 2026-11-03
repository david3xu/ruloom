"""Separate publication controls: ship accepted bytes, recover, deny.

Every ledger control case for the publication half of RP-09 lives
here: the submit-to-download chain over a real engine run, idempotent
re-publication, interrupted-publication recovery, cross-authority
denial, and refusal of foreign, mismatched, or unverifiable bytes.
The engine runs use the tabular profile with a replay double, so no
case needs a live model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from engine.artifacts import publication
from engine.artifacts.hashing import sha256_bytes
from engine.artifacts.publication import PublicationError, Publisher, download
from engine.core.dispatch import AuthorityDenied, DispatchStore
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_OK, execute
from engine.core.spec import normalize_spec
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")

T0 = 2_000_000.0


class FakeModel:
    """A planner double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


def _write_source(path: Path) -> Path:
    csv_path = path / "sample-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "publication-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _accepted_run(out: Path, source: Path) -> tuple[Path, dict[str, Any]]:
    """Run the engine to an accepted release; return (run_dir, outcome)."""
    factory = lambda role, config: FakeModel(  # noqa: E731
        [{"seed": 1, "n_rows": 100}, {"seed": 1, "n_rows": 100}]
    )
    answers = lambda prompt: (  # noqa: E731
        "accept" if prompt.startswith("Decision") else "ok"
    )
    code, outcome = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=factory,
        out=out,
        say=lambda message: None,
        ask=answers,
    )
    assert code == EXIT_OK
    run_dir = out / "runs" / outcome["run_id"]
    assert (run_dir / "release.zip").is_file()
    return run_dir, outcome


def _request_pins(run_dir: Path) -> dict[str, str]:
    """Derive the request pins the run's own record implies (DP-01).

    Reads the run's recorded source path, spec bound, and attempt count
    back out of ``run.json`` and returns the source-content digest and
    normalised spec digest a dispatch request for this exact run must
    declare. The run itself is the oracle: whatever the engine measured
    and normalised is what the binding compares against.
    """
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    request = record["run"]["request"]
    source_path = Path(request["source"])
    _, spec_digest = normalize_spec(
        None,
        MANIFEST,
        source=source_path,
        max_attempts=int(request["max_attempts"]),
    )
    return {
        "source_sha256": sha256_bytes(source_path.read_bytes()),
        "spec_digest": spec_digest,
    }


def _succeeded_item(
    base: Path, run_dir: Path, outcome: Mapping[str, Any], *, key: str = "k1"
) -> tuple[DispatchStore, dict[str, Any]]:
    store = DispatchStore(base)
    item = store.submit(
        authority="team-a",
        request={
            "profile": "tabular",
            "source": "sample-tabular.csv",
            **_request_pins(run_dir),
        },
        idempotency_key=key,
        now=T0,
    )
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    return store, store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )


def _publisher(base: Path) -> Publisher:
    return Publisher(base, base / "published")


def test_publish_then_download_round_trip(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    identity = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    published_zip = Path(identity["dest"])
    assert published_zip.is_file()
    assert identity["artifact_sha256"] == outcome["artifact_sha256"]
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert identity["zip_sha256"] == reviewed
    sidecar = published_zip.parent / f"{published_zip.name}.sha256"
    assert sidecar.read_text(encoding="utf-8") == f"{reviewed}  {published_zip.name}\n"

    fetched = download(published_zip, tmp_path / "fetch" / "release.zip")
    assert fetched["zip_sha256"] == reviewed
    assert fetched["artifact_sha256"] == outcome["artifact_sha256"]
    assert fetched["zip_sha256"] == identity["zip_sha256"]


def test_republish_is_a_noop(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    first = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    before = Path(first["dest"]).read_bytes()
    second = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 10.0
    )
    assert second == first
    assert Path(second["dest"]).read_bytes() == before


def test_interrupted_copy_resumes_to_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    def boom(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
        staged = dest_dir / (dest_name + ".tmp.crash")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(publication, "_stage_and_commit", boom)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    assert not dest.exists()
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "in_progress"

    monkeypatch.undo()
    identity = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 5.0
    )
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert identity["zip_sha256"] == reviewed
    assert publisher.get(item["item_id"])["status"] == "complete"
    assert list((base / "published").glob("*.tmp.*")) == []
    fetched = download(dest, tmp_path / "fetch" / "release.zip")
    assert fetched["zip_sha256"] == reviewed


def test_interrupted_mark_adopts_matching_bytes(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    identity = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    # Simulate a crash after the copy but before the complete mark.
    record_path = base / "publications" / f"{item['item_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["status"] = "in_progress"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    resumed = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 5.0
    )
    assert resumed == identity


def test_publish_denies_a_foreign_authority(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    with pytest.raises(AuthorityDenied):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-b")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_refuses_unsucceeded_or_mismatched_work(
    tmp_path: Path,
) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    store = DispatchStore(base)
    publisher = _publisher(base)

    claimed = store.submit(
        authority="team-a",
        request={"profile": "tabular"},
        idempotency_key="unreported",
        now=T0,
    )
    store.claim(claimed["item_id"], worker="w1", lease_seconds=600.0, now=T0)
    with pytest.raises(PublicationError, match="not succeeded"):
        publisher.publish(claimed["item_id"], run_dir, authority="team-a")

    other_dir, _ = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome, key="bound")
    with pytest.raises(PublicationError, match="not .*"):
        publisher.publish(item["item_id"], other_dir, authority="team-a")

    _, stale = _succeeded_item(base, run_dir, outcome, key="stale")
    (run_dir / "release.zip").write_bytes(
        (run_dir / "release.zip").read_bytes() + b"\x00"
    )
    with pytest.raises(PublicationError, match="does not match the shipped bytes"):
        publisher.publish(stale["item_id"], run_dir, authority="team-a")


def test_publish_refuses_missing_or_broken_releases(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    (run_dir / "release.zip").unlink()
    with pytest.raises(PublicationError, match="no accepted release"):
        publisher.publish(item["item_id"], run_dir, authority="team-a")

    run_dir2, outcome2 = _accepted_run(base, _write_source(tmp_path))
    _, item2 = _succeeded_item(base, run_dir2, outcome2, key="broken")
    raw = bytearray((run_dir2 / "release.zip").read_bytes())
    raw[100] ^= 0xFF
    (run_dir2 / "release.zip").write_bytes(bytes(raw))
    with pytest.raises(PublicationError, match="consumer verification"):
        publisher.publish(item2["item_id"], run_dir2, authority="team-a")


def test_publish_never_overwrites_foreign_bytes(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    dest_dir = base / "published"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{item['item_id']}.zip").write_bytes(b"not our bundle")
    with pytest.raises(PublicationError, match="attributes to this item"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    assert (dest_dir / f"{item['item_id']}.zip").read_bytes() == b"not our bundle"


def test_in_progress_foreign_destination_refuses_and_preserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-02 B02: an in-progress record never adopts foreign bytes.

    An interrupted-before-copy publish leaves a real in-progress
    record with no destination; unrelated bytes planted at the
    destination afterwards must refuse on retry (never overwritten,
    never adopted), the record must stay in-progress, authorised
    repair must refuse (it only restores complete records), and
    removing the foreign bytes must let the retry converge.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    def boom(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
        staged = dest_dir / (dest_name + ".tmp.crash")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(publication, "_stage_and_commit", boom)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    assert not dest.exists()
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "in_progress"
    monkeypatch.undo()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"not our bundle")
    with pytest.raises(PublicationError, match="attributes to this item"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0 + 5.0)
    assert dest.read_bytes() == b"not our bundle"
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "in_progress"
    with pytest.raises(PublicationError, match="no complete publication"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="stray copy",
            now=T0 + 6.0,
        )
    assert dest.read_bytes() == b"not our bundle"

    dest.unlink()
    identity = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 10.0
    )
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert identity["zip_sha256"] == reviewed
    assert publisher.get(item["item_id"])["status"] == "complete"
    assert list((base / "published").glob("*.tmp.*")) == []


def test_one_item_ships_one_release(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    record_path = base / "publications" / f"{item['item_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["zip_sha256"] = "0" * 64
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="already published"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)


def test_download_removes_unverifiable_bundles(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, _ = _accepted_run(base, _write_source(tmp_path))
    broken = tmp_path / "broken.zip"
    raw = bytearray((run_dir / "release.zip").read_bytes())
    raw[100] ^= 0xFF
    broken.write_bytes(bytes(raw))
    target = tmp_path / "fetch" / "release.zip"
    with pytest.raises(PublicationError, match="consumer verification"):
        download(broken, target)
    assert not target.exists()


def test_end_to_end_submit_claim_run_report_publish_download(
    tmp_path: Path,
) -> None:
    base = tmp_path / "out"
    store = DispatchStore(base)
    source_path = _write_source(tmp_path)
    _, spec_digest = normalize_spec(None, MANIFEST, source=source_path, max_attempts=1)
    item = store.submit(
        authority="e2e",
        request={
            "profile": "tabular",
            "source": "sample-tabular.csv",
            "max_attempts": 1,
            "source_sha256": sha256_bytes(source_path.read_bytes()),
            "spec_digest": spec_digest,
        },
        idempotency_key="e2e-1",
        max_retries=1,
        now=T0,
    )
    lease = store.claim(
        item["item_id"], worker="launcher-1", lease_seconds=600.0, now=T0
    )["lease"]

    run_dir, outcome = _accepted_run(base, source_path)
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    item = store.report_success(
        item["item_id"],
        worker="launcher-1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )
    assert item["status"] == "succeeded"

    publisher = Publisher(base, base / "published")
    identity = publisher.publish(item["item_id"], run_dir, authority="e2e", now=T0)
    fetched = download(Path(identity["dest"]), tmp_path / "consumer" / "release.zip")
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert fetched["zip_sha256"] == reviewed == identity["zip_sha256"]
    assert fetched["artifact_sha256"] == outcome["artifact_sha256"]
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert fetched["artifact_sha256"] == run_record["artifact_sha256"]


def _altered_run_dir(
    base: Path, run_dir: Path, outcome: Mapping[str, Any]
) -> tuple[Path, str]:
    """Copy a run but change only validation-report.json to a failure.

    The internal checksums are re-cut around the altered bytes while
    the external manifest still binds the originals, and no new
    acceptance decision or detached hash is created.
    """
    import zipfile

    from engine.artifacts.hashing import sha256_bytes
    from engine.artifacts.package import build_checksums, build_zip_bytes

    with zipfile.ZipFile(run_dir / "release.zip") as archive:
        members = {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "checksums.sha256"
        }
    report = json.loads(members["validation-report.json"])
    report["verdict"] = "fail"
    report["results"] = [
        {
            "name": "audit_counterexample",
            "status": "fail",
            "detail": "This file was changed after acceptance",
        }
    ]
    members["validation-report.json"] = (
        json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    )
    members["checksums.sha256"] = build_checksums(members).encode()
    altered = base / "altered" / outcome["run_id"]
    altered.mkdir(parents=True)
    (altered / "release.zip").write_bytes(build_zip_bytes(members))
    (altered / "manifest.json").write_text(
        (run_dir / "manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return altered, sha256_bytes(build_zip_bytes(members))


def _succeeded_alteration(
    base: Path, altered: Path, outcome: Mapping[str, Any], altered_sha: str
) -> dict[str, Any]:
    """Report the altered bundle through a live lease (worker evidence)."""
    store = DispatchStore(base)
    item = store.submit(
        authority="owner-a",
        request={"profile": "tabular", "source": "sample-tabular.csv"},
        now=T0,
    )
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    return store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": altered_sha,
        },
        now=T0,
    )


def test_publish_refuses_altered_report_with_stale_manifest(
    tmp_path: Path,
) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    altered, altered_sha = _altered_run_dir(base, run_dir, outcome)
    assert (
        altered_sha
        != (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    )
    item = _succeeded_alteration(base, altered, outcome, altered_sha)
    with pytest.raises(PublicationError):
        _publisher(base).publish(item["item_id"], altered, authority="owner-a")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_binds_reported_digests_to_persisted_acceptance(
    tmp_path: Path,
) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    store = DispatchStore(base)
    item = store.submit(
        authority="owner-a",
        request={"profile": "tabular", "source": "sample-tabular.csv"},
        now=T0,
    )
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    item = store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": "0" * 64,
            "zip_sha256": zip_sha,
        },
        now=T0,
    )
    with pytest.raises(PublicationError, match="artifact"):
        _publisher(base).publish(item["item_id"], run_dir, authority="owner-a")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_requires_detached_release_hash(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    (run_dir / "release.sha256").unlink()
    with pytest.raises(PublicationError, match="release.sha256"):
        _publisher(base).publish(item["item_id"], run_dir, authority="owner-a")

    run_dir2, outcome2 = _accepted_run(base, _write_source(tmp_path))
    _, item2 = _succeeded_item(base, run_dir2, outcome2, key="detached2")
    (run_dir2 / "release.sha256").write_text(
        f"{'f' * 64}  release.zip\n", encoding="utf-8"
    )
    with pytest.raises(PublicationError, match="release.sha256"):
        _publisher(base).publish(item2["item_id"], run_dir2, authority="owner-a")


def test_publish_requires_an_accepted_decision_binding(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    decision["verdict"] = "reject"
    (run_dir / "decision.json").write_text(
        json.dumps(decision, indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(PublicationError, match="accept"):
        _publisher(base).publish(item["item_id"], run_dir, authority="owner-a")

    run_dir2, outcome2 = _accepted_run(base, _write_source(tmp_path))
    _, item2 = _succeeded_item(base, run_dir2, outcome2, key="decision2")
    (run_dir2 / "decision.json").unlink()
    with pytest.raises(PublicationError, match="decision"):
        _publisher(base).publish(item2["item_id"], run_dir2, authority="owner-a")


def test_publish_binds_request_profile_to_run_profile(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    store = DispatchStore(base)
    item = store.submit(
        authority="owner-a",
        request={
            "profile": "hylogger",
            "source": "different-source.csv",
            "spec": {"n_rows": 9999},
        },
        now=T0,
    )
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    item = store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )
    with pytest.raises(PublicationError, match="profile"):
        _publisher(base).publish(item["item_id"], run_dir, authority="owner-a")
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_records_required_report_coverage(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    record = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert record["acceptance"]["verdict"] == "pass"
    assert len(record["acceptance"]["required_checks"]) > 1
    assert record["acceptance"]["failed"] == []


def test_publish_refuses_unsatisfied_required_coverage(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    record = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    required = list(record["acceptance"]["required_checks"])
    assert len(required) > 1
    record["acceptance"]["required_checks"] = required + ["no_such_check"]
    (run_dir / "manifest.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(PublicationError, match="required"):
        _publisher(base).publish(item["item_id"], run_dir, authority="owner-a")
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_rejects_traversal_item_ids(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    for bad_id in ("../../escaped", "/absolute", "a/b", "..", ""):
        with pytest.raises((PublicationError, AuthorityDenied)):
            _publisher(base).publish(bad_id, run_dir, authority="team-a")
    assert not (base / "escape-test.json").exists()
    assert list((base / "publications").glob("*.json")) == []


def test_download_failure_preserves_existing_good_destination(
    tmp_path: Path,
) -> None:
    """CP-03 A05: an invalid download never clobbers a good destination."""
    base = tmp_path / "out"
    run_dir, _ = _accepted_run(base, _write_source(tmp_path))
    good_source = run_dir / "release.zip"
    dest = tmp_path / "fetch" / "release.zip"
    first = download(good_source, dest)
    good_bytes = dest.read_bytes()
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert first["zip_sha256"] == reviewed
    assert good_bytes == good_source.read_bytes()

    broken = tmp_path / "broken.zip"
    raw = bytearray(good_source.read_bytes())
    raw[100] ^= 0xFF
    broken.write_bytes(bytes(raw))
    with pytest.raises(PublicationError, match="consumer verification"):
        download(broken, dest)
    assert dest.read_bytes() == good_bytes
    fetched = download(good_source, dest)
    assert fetched["zip_sha256"] == reviewed
    assert dest.read_bytes() == good_bytes


def test_download_interrupted_commit_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP-03 A05: a crash at commit leaves the existing destination intact."""
    import os as _os

    base = tmp_path / "out"
    run_dir, _ = _accepted_run(base, _write_source(tmp_path))
    good_source = run_dir / "release.zip"
    dest = tmp_path / "fetch" / "release.zip"
    download(good_source, dest)
    good_bytes = dest.read_bytes()
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(_os, "replace", boom)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            download(good_source, tmp_path / "fetch" / "release.zip")
    finally:
        monkeypatch.undo()
    assert dest.read_bytes() == good_bytes
    assert list((tmp_path / "fetch").glob("release.zip.tmp.*")) == []
    fetched = download(good_source, dest)
    assert dest.read_bytes() == good_bytes
    assert fetched["zip_sha256"] == reviewed


def _write_source_b(path: Path) -> Path:
    """A second source with different values (hence a different digest)."""
    csv_path = path / "source-b.csv"
    csv_path.write_text("value,label\n9,Z\n8,Y\n7,Z\n6,Y\n", encoding="utf-8")
    return csv_path


def _report(
    store: DispatchStore,
    item_id: str,
    run_dir: Path,
    outcome: Mapping[str, Any],
    *,
    worker: str = "w1",
) -> dict[str, Any]:
    """Report a run through a live lease with genuine digests."""
    lease = store.claim(item_id, worker=worker, lease_seconds=600.0, now=T0)["lease"]
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    return store.report_success(
        item_id,
        worker=worker,
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )


def test_publish_refuses_wrong_source_request_binding(
    tmp_path: Path,
) -> None:
    """DP-01 C01(a): a job for source B must not ship source A's run.

    The run is accepted from source A; a separate job pins source B's
    content digest (same profile) and names the A run with genuine
    hashes. Publication must refuse: profile-name equality alone never
    suffices.
    """
    base = tmp_path / "out"
    source_a = _write_source(tmp_path)
    run_dir, outcome = _accepted_run(base, source_a)
    pins = _request_pins(run_dir)

    source_b = _write_source_b(tmp_path)
    store = DispatchStore(base)
    item = store.submit(
        authority="team-a",
        request={
            "profile": "tabular",
            "source": "source-b.csv",
            "source_sha256": sha256_bytes(source_b.read_bytes()),
            "spec_digest": pins["spec_digest"],
        },
        now=T0,
    )
    item = _report(store, item["item_id"], run_dir, outcome)
    with pytest.raises(PublicationError, match="source"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_refuses_tighter_row_bound(tmp_path: Path) -> None:
    """DP-01 C01(b): a max_rows:1 request must not ship a 100-row run.

    The job carries a GenerationSpec bound of one row against the
    existing 100-row accepted run. The shipped row count must satisfy
    the requested bound, so publication refuses.
    """
    base = tmp_path / "out"
    source_path = _write_source(tmp_path)
    run_dir, outcome = _accepted_run(base, source_path)
    pins = _request_pins(run_dir)
    _, tight_digest = normalize_spec(
        {"limits": {"max_rows": 1}},
        MANIFEST,
        source=source_path,
        max_attempts=1,
    )

    store = DispatchStore(base)
    item = store.submit(
        authority="team-a",
        request={
            "profile": "tabular",
            "source": "sample-tabular.csv",
            "source_sha256": pins["source_sha256"],
            "spec_digest": tight_digest,
            "spec": {"limits": {"max_rows": 1}},
        },
        now=T0,
    )
    item = _report(store, item["item_id"], run_dir, outcome)
    with pytest.raises(PublicationError, match="max_rows"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_refuses_changed_source_bytes(tmp_path: Path) -> None:
    """DP-01 C01(c): bytes changed at the same path after submit refuse.

    The job pins the source digest measured at submit time; the file is
    then rewritten at the same path before the run, so the run measures
    a different digest. Publication must refuse the mismatch.
    """
    base = tmp_path / "out"
    source_path = _write_source(tmp_path)
    _, spec_digest = normalize_spec(None, MANIFEST, source=source_path, max_attempts=1)
    pinned = sha256_bytes(source_path.read_bytes())
    store = DispatchStore(base)
    item = store.submit(
        authority="team-a",
        request={
            "profile": "tabular",
            "source": "sample-tabular.csv",
            "source_sha256": pinned,
            "spec_digest": spec_digest,
        },
        now=T0,
    )
    source_path.write_text("value,label\n5,Q\n6,Q\n7,Q\n8,Q\n", encoding="utf-8")
    assert sha256_bytes(source_path.read_bytes()) != pinned

    run_dir, outcome = _accepted_run(base, source_path)
    item = _report(store, item["item_id"], run_dir, outcome)
    with pytest.raises(PublicationError, match="source"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def _swap_after_acceptance(
    monkeypatch: pytest.MonkeyPatch, run_dir: Path, swapped: bytes
) -> None:
    """Deterministic hook at the acceptance/copy boundary (DP-01 C02).

    Wraps the snapshot-stability guard so the run ZIP is replaced just
    after acceptance resolution and restored in ``finally``. The guard
    itself performs the tripwire read, so the swap is always observed.
    """
    from engine.artifacts import publication as publication_module

    real_guard = publication_module._verify_snapshot_stable
    zip_path = run_dir / "release.zip"
    original = zip_path.read_bytes()

    def hooked(zip_file: Path, snapshot_digest: str) -> None:
        zip_file.write_bytes(swapped)
        try:
            real_guard(zip_file, snapshot_digest)
        finally:
            zip_file.write_bytes(original)

    monkeypatch.setattr(publication_module, "_verify_snapshot_stable", hooked)


def test_publish_refuses_zip_swap_after_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DP-01 C02(d): bytes swapped after acceptance refuse, never ship.

    The run ZIP is replaced after acceptance resolution but before the
    copy. Publication must refuse, leave no destination, and never mark
    the item complete; the published digest never differs from the
    accepted one because nothing is published.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)

    _swap_after_acceptance(monkeypatch, run_dir, b"forged-bytes")
    with pytest.raises(PublicationError, match="changed after acceptance"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    dest = base / "published" / f"{item['item_id']}.zip"
    assert not dest.exists()
    record = _publisher(base).get(item["item_id"])
    assert record is None or record["status"] != "complete"


def test_publish_refuses_well_formed_zip_swap_after_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DP-01 C02(d): even a well-formed different bundle refuses.

    Same boundary as above, but the swapped-in bytes are another valid
    accepted bundle (different digest). The guard compares digests, not
    well-formedness, so publication still refuses with nothing shipped.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    other_dir, _ = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    accepted = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    swapped = (other_dir / "release.zip").read_bytes()
    assert sha256_bytes(swapped) != accepted

    _swap_after_acceptance(monkeypatch, run_dir, swapped)
    with pytest.raises(PublicationError, match="changed after acceptance"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    dest = base / "published" / f"{item['item_id']}.zip"
    assert not dest.exists()
    record = _publisher(base).get(item["item_id"])
    assert record is None or record["status"] != "complete"


def test_download_same_source_and_destination_is_lossless(
    tmp_path: Path,
) -> None:
    """CP-03 A05: same source/destination verifies in place, never deletes."""
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    published = run_dir / "release.zip"
    good_bytes = published.read_bytes()
    identity = download(published, published)
    assert published.read_bytes() == good_bytes
    assert identity["artifact_sha256"] == outcome["artifact_sha256"]

    broken = tmp_path / "broken.zip"
    raw = bytearray(good_bytes)
    raw[100] ^= 0xFF
    broken.write_bytes(bytes(raw))
    broken_bytes = broken.read_bytes()
    with pytest.raises(PublicationError, match="consumer verification"):
        download(broken, broken)
    assert broken.read_bytes() == broken_bytes


def test_republish_refuses_when_complete_destination_missing(tmp_path: Path) -> None:
    """HP-02 A02: a complete record with a missing destination refuses.

    The bytes are gone, so plain re-publication must not silently
    re-copy: recovery goes through the authorised repair operation,
    which records the decision.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    assert dest.is_file()
    dest.unlink()
    with pytest.raises(PublicationError, match="no longer attests"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0 + 10.0)
    assert not dest.exists()
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"


def test_republish_refuses_unexpected_destination_bytes(tmp_path: Path) -> None:
    """HP-02 A02: a complete record with foreign destination bytes refuses.

    Plain re-publication must never overwrite bytes it cannot
    attribute to its own record -- the foreign bytes stay untouched
    and the record stays complete.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    dest.write_bytes(b"not our bundle")
    with pytest.raises(PublicationError, match="no longer attests"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0 + 10.0)
    assert dest.read_bytes() == b"not our bundle"
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"


def test_repair_restores_missing_destination_and_records_decision(
    tmp_path: Path,
) -> None:
    """HP-02 A02: authorised repair restores a missing destination.

    The repair restores the recorded bytes, keeps the record
    complete, and appends the authorising decision (who, when, why,
    what was found) -- after which plain re-publication is a no-op
    again.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    first = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    shipped = dest.read_bytes()
    dest.unlink()
    with pytest.raises(PublicationError, match="without a reason"):
        publisher.repair(
            item["item_id"], run_dir, authority="team-a", reason="  ", now=T0 + 10.0
        )
    with pytest.raises(AuthorityDenied):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-b",
            reason="restore after disk loss",
            now=T0 + 10.0,
        )
    assert not dest.exists()
    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after disk loss",
        now=T0 + 20.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert dest.read_bytes() == shipped
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    entry = repairs[0]
    assert entry["authority"] == "team-a"
    assert entry["reason"] == "restore after disk loss"
    assert entry["found"] == "absent"
    assert entry["restored"] == first["zip_sha256"]
    assert entry["at"] == T0 + 20.0
    second = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 30.0
    )
    assert second["zip_sha256"] == first["zip_sha256"]


def test_repair_preserves_replaced_bytes_and_records_decision(
    tmp_path: Path,
) -> None:
    """HP-02 A02: repair quarantines unexpected bytes before restoring.

    The replaced file is preserved beside the destination under a
    digest-derived name (never silently dropped), the recorded bytes
    are restored, and the decision entry names what was found and
    where the replaced bytes went.
    """
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    first = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    shipped = dest.read_bytes()
    dest.write_bytes(b"not our bundle")
    foreign_digest = sha256_bytes(b"not our bundle")
    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="destination overwritten by stray copy",
        now=T0 + 10.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert dest.read_bytes() == shipped
    preserved = dest.parent / f"{dest.name}.replaced.{foreign_digest}"
    assert preserved.read_bytes() == b"not our bundle"
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    entry = repairs[0]
    assert entry["found"] == foreign_digest
    assert entry["preserved"] == preserved.name
    assert entry["restored"] == first["zip_sha256"]
    assert entry["reason"] == "destination overwritten by stray copy"


def _published_then_diverged(
    base: Path, tmp_path: Path, *, foreign: bytes = b"not our bundle"
) -> tuple:
    """Publish once, then plant foreign bytes at the destination.

    Returns ``(run_dir, outcome, item, publisher, dest, shipped, first)``
    where ``shipped`` is the attested bytes and ``first`` the publish
    identity. Shared setup for the IP-03 durable-repair tests.
    """
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    first = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    shipped = dest.read_bytes()
    dest.write_bytes(foreign)
    return run_dir, outcome, item, publisher, dest, shipped, first


def test_repair_persists_intent_before_copy_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-03 B03: the repair intent is durable before any byte moves.

    A crash at the copy leaves the intent in the record (who, when,
    why, what was found, what will be restored, where the replaced
    bytes went) with no repairs entry yet; the replaced bytes are
    already quarantined. A retry resumes from that intent and records
    exactly one entry carrying the original authorisation.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path
    )
    foreign_digest = sha256_bytes(b"not our bundle")

    def boom(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(publication, "_stage_and_commit", boom)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    assert record.get("repairs", []) == []
    pending = record.get("pending_repair")
    assert isinstance(pending, dict)
    assert pending["authority"] == "team-a"
    assert pending["reason"] == "restore after stray copy"
    assert pending["found"] == foreign_digest
    assert pending["restored"] == first["zip_sha256"]
    assert pending["at"] == T0 + 10.0
    assert not dest.exists()
    preserved = dest.parent / f"{dest.name}.replaced.{foreign_digest}"
    assert preserved.read_bytes() == b"not our bundle"
    monkeypatch.undo()

    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after stray copy",
        now=T0 + 20.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert dest.read_bytes() == shipped
    assert preserved.read_bytes() == b"not our bundle"
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    assert "pending_repair" not in record
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    assert repairs[0]["found"] == foreign_digest
    assert repairs[0]["preserved"] == preserved.name
    assert repairs[0]["at"] == T0 + 10.0
    assert list((base / "published").glob("*.tmp.*")) == []


def test_repair_new_arrival_refuses_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JP-01 C01: a retry after new bytes arrive mid-repair refuses.

    The persisted intent authorises only its recorded observed bytes
    plus the accepted replacement: after the quarantine move, a new
    arrival at the destination is a third digest, so the retry
    refuses without mutating anything -- the new bytes stay at the
    destination, the earlier quarantine stays intact, and the pending
    intent and repair history are unchanged. Once the new arrival is
    separately set aside, the original intent still resumes to a
    single entry.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path, foreign=b"stray copy A"
    )
    found_digest = sha256_bytes(b"stray copy A")

    def boom(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(publication, "_stage_and_commit", boom)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    monkeypatch.undo()
    assert not dest.exists()
    preserved = dest.parent / f"{dest.name}.replaced.{found_digest}"
    assert preserved.read_bytes() == b"stray copy A"

    dest.write_bytes(b"unrelated new arrival")
    with pytest.raises(PublicationError, match="already in progress"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="retry after new arrival",
            now=T0 + 20.0,
        )
    assert dest.read_bytes() == b"unrelated new arrival"
    assert preserved.read_bytes() == b"stray copy A"
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    assert record.get("repairs", []) == []
    pending = record.get("pending_repair")
    assert isinstance(pending, dict)
    assert pending["authority"] == "team-a"
    assert pending["reason"] == "restore after stray copy"
    assert pending["found"] == found_digest
    assert pending["restored"] == first["zip_sha256"]
    assert pending["preserved"] == preserved.name
    assert pending["at"] == T0 + 10.0
    assert list((base / "published").glob("*.tmp.*")) == []

    dest.unlink()
    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="retry after new arrival set aside",
        now=T0 + 30.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert dest.read_bytes() == shipped
    assert preserved.read_bytes() == b"stray copy A"
    record = publisher.get(item["item_id"])
    assert record is not None and "pending_repair" not in record
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    assert repairs[0]["found"] == found_digest
    assert repairs[0]["restored"] == first["zip_sha256"]


def test_repair_crash_during_preserve_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-03 B03: a crash while quarantining leaves intent, not damage.

    The intent is already durable, the destination still holds the
    foreign bytes, and no quarantine exists yet. A retry quarantines,
    restores, and records exactly one entry.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path
    )
    foreign_digest = sha256_bytes(b"not our bundle")

    def boom(dest_path: Path, preserved_path: Path, *, found: str) -> None:
        raise RuntimeError("injected preserve failure")

    monkeypatch.setattr(publication, "_quarantine_replaced", boom)
    with pytest.raises(RuntimeError, match="injected preserve failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    assert record.get("repairs", []) == []
    pending = record.get("pending_repair")
    assert isinstance(pending, dict) and pending["found"] == foreign_digest
    assert dest.read_bytes() == b"not our bundle"
    assert not (dest.parent / f"{dest.name}.replaced.{foreign_digest}").exists()
    monkeypatch.undo()

    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after stray copy",
        now=T0 + 20.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert dest.read_bytes() == shipped
    record = publisher.get(item["item_id"])
    assert record is not None and "pending_repair" not in record
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    assert repairs[0]["found"] == foreign_digest


def test_repair_crash_at_sidecar_finalises_without_recopy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-03 B03: a crash after the copy finalises idempotently.

    The destination already matches, so the retry must not copy again
    (the ZIP-already-matches path): it finalises the pending intent
    into exactly one repairs entry.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path
    )

    real_write = publication._atomic_write_bytes

    def boom(path: Path, payload: bytes) -> Path:
        if path.name.endswith(".sha256"):
            raise RuntimeError("injected sidecar failure")
        return real_write(path, payload)

    monkeypatch.setattr(publication, "_atomic_write_bytes", boom)
    with pytest.raises(RuntimeError, match="injected sidecar failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    assert dest.read_bytes() == shipped
    record = publisher.get(item["item_id"])
    assert record is not None and isinstance(record.get("pending_repair"), dict)
    assert record.get("repairs", []) == []
    monkeypatch.undo()

    copies = {"n": 0}
    real_stage = publication._stage_and_commit

    def counting(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
        copies["n"] += 1
        return real_stage(dest_dir, dest_name, payload)

    monkeypatch.setattr(publication, "_stage_and_commit", counting)
    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after stray copy",
        now=T0 + 20.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert copies["n"] == 0
    record = publisher.get(item["item_id"])
    assert record is not None and "pending_repair" not in record
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    assert repairs[0]["restored"] == first["zip_sha256"]


def test_repair_crash_at_final_record_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-03 B03: a crash at the final record write records once.

    The bytes and sidecar are already restored; the intent is still
    pending. A retry appends exactly one entry and clears the intent,
    and a further repair is a no-op (never a duplicate entry).
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path
    )

    real_write_json = publication._atomic_write_json
    calls = {"n": 0}

    def boom(path: Path, record: object) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected final-record failure")
        return real_write_json(path, record)  # type: ignore[arg-type]

    monkeypatch.setattr(publication, "_atomic_write_json", boom)
    with pytest.raises(RuntimeError, match="injected final-record failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    assert dest.read_bytes() == shipped
    record = publisher.get(item["item_id"])
    assert record is not None and isinstance(record.get("pending_repair"), dict)
    assert record.get("repairs", []) == []
    monkeypatch.undo()

    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after stray copy",
        now=T0 + 20.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    record = publisher.get(item["item_id"])
    assert record is not None and "pending_repair" not in record
    assert isinstance(record.get("repairs"), list) and len(record["repairs"]) == 1

    again = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after stray copy",
        now=T0 + 30.0,
    )
    assert again["zip_sha256"] == first["zip_sha256"]
    record = publisher.get(item["item_id"])
    assert record is not None and len(record["repairs"]) == 1


def test_repair_crash_at_intent_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-03 B03: a crash at the intent write mutates nothing.

    The first record write fails, so the destination, the quarantine
    area, and the record are all untouched. A retry converges with one
    entry.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path
    )
    foreign_digest = sha256_bytes(b"not our bundle")

    real_write_json = publication._atomic_write_json
    calls = {"n": 0}

    def boom(path: Path, record: object) -> Path:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected intent failure")
        return real_write_json(path, record)  # type: ignore[arg-type]

    monkeypatch.setattr(publication, "_atomic_write_json", boom)
    with pytest.raises(RuntimeError, match="injected intent failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    assert dest.read_bytes() == b"not our bundle"
    assert not (dest.parent / f"{dest.name}.replaced.{foreign_digest}").exists()
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "complete"
    assert "pending_repair" not in record
    assert record.get("repairs", []) == []
    monkeypatch.undo()

    identity = publisher.repair(
        item["item_id"],
        run_dir,
        authority="team-a",
        reason="restore after stray copy",
        now=T0 + 20.0,
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    assert dest.read_bytes() == shipped
    record = publisher.get(item["item_id"])
    assert record is not None and "pending_repair" not in record
    assert isinstance(record.get("repairs"), list) and len(record["repairs"]) == 1


def test_repair_validates_history_before_mutating(tmp_path: Path) -> None:
    """IP-03 B03: malformed repair history refuses before any byte moves.

    A corrupt ``repairs`` list or a corrupt pending intent refuses
    without quarantining, copying, or persisting a new intent -- the
    foreign destination and the record are left exactly as found.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, _, _ = _published_then_diverged(base, tmp_path)
    record_path = base / "publications" / f"{item['item_id']}.json"

    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["repairs"] = "corrupt"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="malformed"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    assert dest.read_bytes() == b"not our bundle"
    assert "pending_repair" not in publisher.get(item["item_id"])

    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["repairs"] = []
    record["pending_repair"] = {"bogus": True}
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="malformed"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 20.0,
        )
    assert dest.read_bytes() == b"not our bundle"
    assert publisher.get(item["item_id"])["pending_repair"] == {"bogus": True}


def test_publish_finalises_interrupted_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-03 B03: plain publication finalises a pending repair intent.

    A repair interrupted after the copy leaves matching bytes with the
    intent still pending. A later plain publish (no divergence left to
    repair) finalises that intent into one entry instead of leaving
    the authorisation dangling.
    """
    base = tmp_path / "out"
    run_dir, _, item, publisher, dest, shipped, first = _published_then_diverged(
        base, tmp_path
    )

    real_write_json = publication._atomic_write_json
    calls = {"n": 0}

    def boom(path: Path, record: object) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected final-record failure")
        return real_write_json(path, record)  # type: ignore[arg-type]

    monkeypatch.setattr(publication, "_atomic_write_json", boom)
    with pytest.raises(RuntimeError, match="injected final-record failure"):
        publisher.repair(
            item["item_id"],
            run_dir,
            authority="team-a",
            reason="restore after stray copy",
            now=T0 + 10.0,
        )
    assert dest.read_bytes() == shipped
    assert isinstance(publisher.get(item["item_id"]).get("pending_repair"), dict)
    monkeypatch.undo()

    identity = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 20.0
    )
    assert identity["zip_sha256"] == first["zip_sha256"]
    record = publisher.get(item["item_id"])
    assert record is not None and "pending_repair" not in record
    repairs = record.get("repairs")
    assert isinstance(repairs, list) and len(repairs) == 1
    assert repairs[0]["reason"] == "restore after stray copy"
